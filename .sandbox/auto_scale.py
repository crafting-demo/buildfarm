#!/usr/bin/python3

"""
Buildfarm auto-scaling reference implementation.

This is a reference implementation for adding/removing buildfarm
workers from the sandbox based on the queue-length.
This script must run from inside a sandbox that's defined for running
the buildfarm.
The sandbox will not be linked to any template once it's updated.
"""

import time
import sys
import os
import subprocess
import json
import datetime
import io
import urllib.request


# The URL for collecting metrics from the buildfarm server.
BF_SERVER_METRICS_URL="http://bf-server:9090/metrics"


def _int_from_env_or(key, default):
    """Load integer from env or use the default."""
    val = os.getenv(key)
    if val is None:
        return default
    return int(val)


def _fetch_url(url: str) -> str:
    """Returns the payload from an HTTP GET request."""
    with urllib.request.urlopen(url) as reply:
        return reply.read()


def _cs_sandbox(args: list[str], **kwargs):
    """A helper to invoke the CLI."""
    result = subprocess.run(["cs", "-o", "json", "sandbox"] + args, text=True,check=True,**kwargs)
    if result.returncode != 0:
        raise CLIError(result.returncode, result.stderr)
    return result.stdout


def _workers_in_template(template):
    """
    Extract the list of worker containers from the template.

    The worker containers are named "bf-worker-N" where N is from [0, max_workers).
    """
    return list(filter(
        lambda container: container["name"].startswith("bf-worker-"),
        template["containers"]))


class CLIError(Exception):
    """Failed to execute CLI command."""

    def __init__(self, exit_code: int, stderr: str):
        Exception.__init__(self, f'CLI exited {exit_code}: {stderr}')
        self.exit_code = exit_code
        self.stderr = stderr


class ParameterError(Exception):
    """Invalid parameter in configuration."""

    def __init__(self, reason: str):
        Exception.__init__(self, f'Invalid config: {reason}')


class ValuesByKey:
    """
    The metric values by key.
    The value mapping to a key is a list of floating numbers
    from the metrics named by the key.
    """

    values_by_key = {}

    def add(self, key: str, value: float):
        """Add a new value."""
        values = self.values_by_key.get(key, [])
        values.append(value)
        self.values_by_key[key] = values


    def sum(self, key: str) -> float:
        """Sum up all the values of the specified key."""
        return sum(self.values_by_key.get(key, []))


def scrape_metrics_from(server_url: str) -> ValuesByKey:
    """
    Scrape and prometheus metrics by key.

    The scraped prometheus metrics are lines of:
    key{label1=value, label2=value...} VALUE
    ...

    The values from the same key are appended to the same list
    with labels discarded.

    For example, the original metrics are:
    pool_size{pool="pool1"} 5
    pool_size{pool="pool2"} 3

    The returned ValuesByKey contains:
    {
        "pool_size": [5, 3]
    }
    """
    buf = io.StringIO(_fetch_url(server_url))
    values_by_key = ValuesByKey()
    while True:
        line = buf.readline()
        if len(line) == 0:
            break
        if line.startswith("#"):
            continue
        # Trim begin and end space
        line = line.strip()
        try:
            index = line.rindex(" ")
        except ValueError:
            continue
        key = line[:index]
        value = line[index+1:]
        try:
            label_index = key.index("{")
            key = key[:label_index]
        except ValueError:
            # Ignore the exception.
            pass
        values_by_key.add(key, float(value))
    return values_by_key


class AutoScaler:
    """
    The auto-scale controller of the buildfarm workers.

    The "queue_size" metric from the server is used to decide whether additional
    workers are needed. When queue_size is positive, it's the time to add workers.
    When it's zero, attempts are made to remove idle workers.

    For avoid overrunning and keep the action stable, a "wait_period" is introduced.
    When the scaling condition is detected, "scaling_demanded_at" is set to a valid
    time as a starting point. After time elapsed by "wait_period", and the scaling
    condition still stand, the scaling action will be attempted.

    The min/max numbers of workers are always required. And the min_workers must not
    be smaller than 1.
    """

    # Following are the parameters for auto-scaling.

    # The minimum number of workers.
    min_workers = _int_from_env_or("BF_MIN_WORKERS", 1)

    # The maximum number of workers.
    max_workers = _int_from_env_or("BF_MAX_WORKERS", 4)

    # The seconds to wait before taking the action since
    # the scaling is demanded.
    wait_period = _int_from_env_or("BF_WAIT_PERIOD", 60)

    # The seconds between scrapes of the metrics.
    scrape_interval = _int_from_env_or("BF_SCRAPE_INTERVAL", 5)

    # The seconds of delay before next retry.
    retry_delay = _int_from_env_or("BF_RETRY_DELAY", 1)

    # Following are the states for controling the scaling.

    # If scale-out is demanded - not sufficient workers.
    scale_out_demanded = True

    # Scale demanded time.
    scaling_demanded_at: datetime.datetime | None = None

    def __init__(self):
        sandbox_name = os.getenv("SANDBOX_FULL_NAME")
        if sandbox_name == "" or sandbox_name is None:
            raise ParameterError("sandbox name unknown")
        self._sandbox_name = sandbox_name

        if self.min_workers < 1:
            raise ParameterError(f"min_workers {self.min_workers} < 1")
        if self.max_workers < self.min_workers:
            raise ParameterError(f"max_workers {self.max_workers} < min_workers {self.min_workers}")


    def monitor_loop(self):
        """The loop to scrape metrics and perform auto-scaling."""
        while True:
            try:
                self.scrape_and_scale()
                time.sleep(self.scrape_interval)
            except Exception as error: # pylint: disable=broad-except
                print(str(error), file=sys.stderr)
                time.sleep(self.retry_delay)


    def scrape_and_scale(self):
        """Scrape the metrics and perform auto-scaling if needed."""

        server_metrics = scrape_metrics_from(BF_SERVER_METRICS_URL)
        queue_size = server_metrics.sum("queue_size")
        worker_pool_size = server_metrics.sum("worker_pool_size")

        now = datetime.datetime.now()

        # When scaling_demanded_at is not None, wait_period is activated.
        # Once wait_period elapsed, scaling action will be attempted.
        # If scaling_demanded_at is None, no action should be performed.

        # Calculate scaling demand:
        # - demand scale-out if queue_size is greater than 0.
        # - otherwise, after wait_delay attempt to remove idle workers.
        # This only sets/cancels the scaling demand, the actual action will
        # be performed after wait_period elapsed.
        if queue_size > 0:
            if not self.scale_out_demanded:
                print(f"Scale-out demanded for queue_size={queue_size}")
                self.scale_out_demanded = True
                self.scaling_demanded_at = now
        elif self.scaling_demanded_at is None:
            # It's time to attempte idle worker removal.
            self.scale_out_demanded = False
            self.scaling_demanded_at = now

        # After wait_period, attempt the scaling action.
        scale_worker_num = 0
        if self.scaling_demanded_at is not None \
            and (now - self.scaling_demanded_at).total_seconds() >= self.wait_period:
            if self.scale_out_demanded:
                scale_worker_num = 1
            else:
                scale_worker_num = -1

            # Attempt has been made, reset scaling_demanded_at.
            self.scaling_demanded_at = None

            print(f"Scaling attempt: {scale_worker_num}/{worker_pool_size}")

        if scale_worker_num > 0:
            self.scale_out(scale_worker_num)

        if scale_worker_num < 0:
            self.scale_in(-scale_worker_num)


    def scale_out(self, count):
        """Scale out by adding the specified number of workers."""

        # Retrieve the current definition of the sandbox and extract the workers.
        template = self._sandbox_template()
        workers_in_template = _workers_in_template(template)

        desired_num = len(workers_in_template) + count
        if desired_num > self.max_workers:
            desired_num = self.max_workers
        count = desired_num - len(workers_in_template)
        if count <= 0:
            return
        for _ in range(0, count):
            self._add_worker_to_template(template)
        _cs_sandbox(["edit", "--force", "--from", "-"], input=json.dumps(template))


    def scale_in(self, count):
        """Scale in by removing the specified number of workers."""

        # Retrieve the current definition of the sandbox and extract the workers.
        template = self._sandbox_template()
        workers_in_template = _workers_in_template(template)

        desired_num = len(workers_in_template) - count
        if desired_num < self.min_workers:
            desired_num = self.min_workers
        count = len(workers_in_template) - desired_num
        if count <= 0:
            return

        # Find all idle workers.
        idle_workers = []
        for worker in workers_in_template:
            values = scrape_metrics_from(f"http://{worker['name']}:9090/metric")
            if values.sum("execution_slot_usage") == 0:
                idle_workers.append(worker["name"])

        if len(idle_workers) == 0:
            return

        idle_worker_names = {x: True for x in idle_workers[:count]}
        template["containers"] = list(filter(
            lambda container : container["name"] not in idle_worker_names,
            template["containers"]))
        _cs_sandbox(["edit", "--force", "--from", "-"], input=json.dumps(template))


    def _add_worker_to_template(self, template):
        workers = _workers_in_template(template)
        worker_by_names = {x["name"]: x for x in workers}
        # Workers are named as "bf-worker-N" where N is from [0..max_workers).
        for i in range(0, self.max_workers):
            worker_name = "bf-worker-"+str(i)
            if worker_name not in worker_by_names:
                # Worker containers share the same definition, copy the first one.
                worker = workers[0].copy()
                worker["name"] = worker_name
                template["containers"].append(worker)
                return


    def _sandbox_template(self):
        return json.loads(_cs_sandbox(["show", self._sandbox_name, "--def"], capture_output=True))


if __name__ == "__main__":
    AutoScaler().monitor_loop()
