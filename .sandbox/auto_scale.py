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
import os
import subprocess
import json
import datetime
import io
import urllib.request
import logging
import traceback


def _fetch_url(url: str) -> str:
    """Returns the payload from an HTTP GET request."""
    with urllib.request.urlopen(url) as reply:
        return reply.read().decode('utf-8')


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

    def __init__(self):
        self.values_by_key = {}

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


    # pylint: disable=too-many-instance-attributes
    # These are parameters for auto-scaling.

    def __init__(self):
        self.sandbox_name = os.environ.get("SANDBOX_FULL_NAME", "")
        if self.sandbox_name == "":
            raise ParameterError("sandbox name unknown")

        self.server_url = os.environ.get("BF_SERVER_URL", "http://bf-server:9090/metrics")

        # Following are the parameters for auto-scaling.

        # The minimum number of workers.
        self.min_workers = int(os.environ.get("BF_MIN_WORKERS", "1"))
        if self.min_workers < 1:
            raise ParameterError(f"min_workers {self.min_workers} < 1")

        # The maximum number of workers.
        self.max_workers = int(os.environ.get("BF_MAX_WORKERS", "4"))
        if self.max_workers < self.min_workers:
            raise ParameterError(f"max_workers {self.max_workers} < min_workers {self.min_workers}")

        # The seconds to wait before taking the action since scale-out is demanded.
        # This value can be relatively small for more aggresive scale-out.
        self.wait_scale_out = int(os.environ.get("BF_WAIT_SCALE_OUT", "5"))

        # The seconds to wait before taking the action since scale-in is demanded.
        # This value can be relatively large for less aggresive scale-in.
        self.wait_scale_in = int(os.environ.get("BF_WAIT_SCALE_IN", "60"))

        # The seconds to cool-down before next scale-out can happen.
        self.scale_out_cool_down = int(os.environ.get("BF_SCALE_OUT_COOL_DOWN", "60"))

        # The seconds between scrapes of the metrics.
        self.scrape_interval = int(os.environ.get("BF_SCRAPE_INTERVAL", "5"))

        # The seconds of delay before next retry.
        self.retry_delay = int(os.environ.get("BF_RETRY_DELAY", "1"))

        # Following are the states for controling the scaling.

        # If scale-out/in is demanded.
        # This is the time with wait and cool-down taken into consideration.
        # The actual scaling should not happen before this time.
        self._scale_out_after: datetime.datetime | None = None
        self._scale_in_after: datetime.datetime | None = None

        # The scale-out cool-down time.
        # Scale-out should NOT be demanded before this time.
        self._scale_out_cool_down_before = datetime.datetime.now()


    def monitor_loop(self):
        """The loop to scrape metrics and perform auto-scaling."""
        while True:
            try:
                self.scrape_and_scale()
                time.sleep(self.scrape_interval)
            except Exception as error: # pylint: disable=broad-except
                traceback.print_exception(error)
                time.sleep(self.retry_delay)


    def scrape_and_scale(self):
        """Scrape the metrics and perform auto-scaling if needed."""

        server_metrics = scrape_metrics_from(self.server_url)
        queue_size = server_metrics.sum("queue_size")

        now = datetime.datetime.now()

        logging.debug("Scrape queue_size=%d", queue_size)

        # When scale-out/in is demanded, wait period is activated.
        # After the wait_period elapsed, scaling action will be attempted.

        # Calculate scaling demand:
        # - demand scale-out if queue_size is greater than 0.
        # - otherwise, always cancel scale-out demands and request for scale-in demand.
        if queue_size > 0:
            if self._scale_out_after is None and self._scale_out_cool_down_before < now:
                self._scale_out_after = now + datetime.timedelta(seconds=self.wait_scale_out)
            self._scale_in_after = None
        else:
            if self._scale_in_after is None:
                self._scale_in_after = now + datetime.timedelta(seconds=self.wait_scale_in)
            self._scale_out_after = None

        # Evaluate wait period, attempt the scaling action if it elapsed.
        # Always evaluate scale-out with higher priority.
        if self._scale_out_after is not None:
            if self._scale_out_after < now:
                self.scale_out(1)
                self._scale_out_after = None
                self._scale_out_cool_down_before = datetime.datetime.now() + \
                    datetime.timedelta(seconds=self.scale_out_cool_down)
        elif self._scale_in_after is not None and self._scale_in_after < now:
            self.scale_in(1)
            self._scale_in_after = None


    def scale_out(self, count):
        """Scale out by adding the specified number of workers."""

        # Retrieve the current definition of the sandbox and extract the workers.
        template = self._sandbox_template()
        workers_in_template = _workers_in_template(template)

        desired_num = min(len(workers_in_template) + count, self.max_workers)
        count = desired_num - len(workers_in_template)
        if count <= 0:
            return
        for _ in range(0, count):
            self._add_worker_to_template(template)
        logging.info("Add %d workers", count)
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

        idle_workers.reverse()
        idle_worker_names = {x: True for x in idle_workers[:count]}
        template["containers"] = list(filter(
            lambda container : container["name"] not in idle_worker_names,
            template["containers"]))
        logging.info("Remove idle workers: %s", ','.join(idle_worker_names.keys()))
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
        return json.loads(_cs_sandbox(["show", self.sandbox_name, "--def"], capture_output=True))


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    AutoScaler().monitor_loop()
