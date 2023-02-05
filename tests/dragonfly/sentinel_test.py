import subprocess
from typing import Awaitable
import aioredis
import pytest
import time
import asyncio


# Helper function to parse some sentinel cli commands output as key value dictionaries.
# Output is expected be of even number of lines where each pair of consecutive lines results in a single key value pair.
# If new_dict_key is not empty, encountering it in the output will start a new dictionary, this let us return multiple
# dictionaries, for example in the 'slaves' command, one dictionary for each slave.
def stdout_as_list_of_dicts(cp: subprocess.CompletedProcess, new_dict_key =""):
    lines = cp.stdout.splitlines()
    res = []
    d = None
    if (new_dict_key == ''):
        d = dict()
        res.append(d)
    for i in range(0, len(lines), 2):
        if (lines[i]) == new_dict_key: # assumes output never has '' as a key
            d = dict()
            res.append(d)
        d[lines[i]] = lines[i + 1]
    return res

def wait_for(func, pred, timeout_sec, timeout_msg=""):
    while not pred(func()):
        assert timeout_sec > 0, timeout_msg
        timeout_sec = timeout_sec - 1
        time.sleep(1)

async def await_for(func, pred, timeout_sec, timeout_msg=""):
    done = False
    while not done:
        val = func()
        if isinstance(val, Awaitable):
            val = await val
        done = pred(val)
        assert timeout_sec > 0, timeout_msg
        timeout_sec = timeout_sec - 1
        await asyncio.sleep(1)


class Sentinel:
    def __init__(self) -> None:
        self.port = 5555
        self.image = "bitnami/redis-sentinel:latest"
        self.container_name = "sentinel_test_py_sentinel"
        self.default_deployment = "my_deployment"
        self.initial_master_port = 1111

    def start(self):
        self.stop() # cleanup potential zombies

        cmd = [
            "docker", "run",
            "--network=host",
            "-d",
            f"--name={self.container_name}",
            "-e", "REDIS_MASTER_HOST=localhost",
            "-e", f"REDIS_MASTER_PORT_NUMBER={self.initial_master_port}",
            "-e", f"REDIS_MASTER_SET={self.default_deployment}",
            "-e", f"REDIS_SENTINEL_PORT_NUMBER={self.port}",
            "-e", "REDIS_SENTINEL_QUORUM=1",
            "-e", "REDIS_SENTINEL_DOWN_AFTER_MILLISECONDS=3000",
            "bitnami/redis-sentinel:latest"]
        assert subprocess.run(cmd).returncode == 0, "docker run failed."

    def stop(self):
        assert subprocess.run(["docker", "rm", "-f", "-v", f"{self.container_name}"]).returncode == 0, "docker rm failed."

    def run_cmd(self, args, sentinel_cmd=True, capture_output=False, assert_ok=True) -> subprocess.CompletedProcess:
        run_args = ["redis-cli", "-p", f"{self.port}"]
        if sentinel_cmd: run_args = run_args + ["sentinel"]
        run_args = run_args + args
        cp = subprocess.run(run_args, capture_output=capture_output, text=True)
        if assert_ok:
            assert cp.returncode == 0, f"Command failed: {run_args}"
        return cp

    def wait_ready(self):
        wait_for(
            lambda: self.run_cmd(["ping"], sentinel_cmd=False, assert_ok=False),
            lambda cp:cp.returncode == 0,
            timeout_sec=10, timeout_msg="Timeout waiting for sentinel to become ready.")

    def master(self, deployment="") -> dict:
        if deployment == "":
            deployment = self.default_deployment
        cp = self.run_cmd(["master", deployment], capture_output=True)
        return stdout_as_list_of_dicts(cp)[0]

    def slaves(self, deployment="") -> dict:
        if deployment == "":
            deployment = self.default_deployment
        cp = self.run_cmd(["slaves", deployment], capture_output=True)
        return stdout_as_list_of_dicts(cp)

    def live_master_port(self, deployment=""):
        if deployment == "":
            deployment = self.default_deployment
        cp = self.run_cmd(["get-master-addr-by-name", deployment], capture_output=True)
        return int(cp.stdout.splitlines()[1])

    def failover(self, deployment=""):
        if deployment == "":
            deployment = self.default_deployment
        self.run_cmd(["failover", deployment,])


@pytest.fixture(scope="function") # Sentinel has state which we don't want carried over form test to test.
def sentinel() -> Sentinel:
    s = Sentinel()
    s.start()
    s.wait_ready()
    yield s
    s.stop()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Fails on CI")
async def test_failover(df_local_factory, sentinel):
    master = df_local_factory.create(port=sentinel.initial_master_port)
    replica = df_local_factory.create(port=master.port + 1)

    master.start()
    replica.start()

    master_client = aioredis.Redis(port=master.port)
    replica_client = aioredis.Redis(port=replica.port)

    await replica_client.execute_command("REPLICAOF localhost " + str(master.port))

    assert sentinel.live_master_port() == master.port

    # Verify sentinel picked up replica.
    await await_for(
            lambda: sentinel.master(),
            lambda m: m["num-slaves"] == "1",
            timeout_sec=15, timeout_msg="Timeout waiting for sentinel to pick up replica.")

    sentinel.failover()

    # Verify sentinel switched.
    await await_for(
        lambda: sentinel.live_master_port(),
        lambda p: p == replica.port,
        timeout_sec=3, timeout_msg="Timeout waiting for sentinel to report replica as master."
    )
    assert sentinel.slaves()[0]["port"] == str(master.port)

    # Verify we can now write to replica and read replicated value from master.
    await replica_client.set("key", "value")
    await await_for(
        lambda: master_client.get("key"),
        lambda val: val == b"value",
        10, "Timeout waiting for key to exist in replica."
    )


@pytest.mark.asyncio
@pytest.mark.skip(reason="Fails on CI")
async def test_master_failure(df_local_factory, sentinel):
    master = df_local_factory.create(port=sentinel.initial_master_port)
    replica = df_local_factory.create(port=master.port + 1)

    master.start()
    replica.start()

    replica_client = aioredis.Redis(port=replica.port)

    await replica_client.execute_command("REPLICAOF localhost " + str(master.port))

    assert sentinel.live_master_port() == master.port

    # Verify sentinel picked up replica.
    await await_for(
            lambda: sentinel.master(),
            lambda m: m["num-slaves"] == "1",
            timeout_sec=15, timeout_msg="Timeout waiting for sentinel to pick up replica.")

    # Simulate master failure.
    master.stop()

    # Verify replica pormoted.
    await await_for(
        lambda: sentinel.live_master_port(),
        lambda p: p == replica.port,
        timeout_sec=1000, timeout_msg="Timeout waiting for sentinel to report replica as master."
    )

    # Verify we can now write to replica.
    await replica_client.set("key", "value")
    assert await replica_client.get("key") == b"value"