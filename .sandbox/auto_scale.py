#!/usr/bin/python3

import time
import sys
import os
import subprocess
import json
import datetime
import functools
import requests
import io

# How often to scrape metrics and try to scale. Could config by setting environment variable BF_SCRAPE_PERIOD=xxx
scrape_period=5 
# When an error occurs, the delay of retrying. Unit is sceond.
delay_of_retry=1
# Start to scale up/down when the trend of scaliing up/down  has lasted for the period of time. Unit is second
trending_duration = 10
# Minimal number of buildfarm worker. Could config by setting environment variable BF_MIN_WORKER=xxx. The value must greater than 0
min_worker = 1
# Maximal number fo buildfarm worker. Could config by setting environment variable BF_MAX_WORKER=xxx. The value muster greater equal than min_worker
max_worker = 4

bf_server_metrics_url="http://bf-server:9090/metrics"

# A class to store global infromation.
class Global:
    sandbox_name = ""
    expected_worker_num = 1
    is_scale_up_trending = True
    trending_start_time = datetime.datetime.now()
    def __init__(self):
        self.sandbox_name = os.getenv("SANDBOX_NAME")
        if self.sandbox_name == "" or self.sandbox_name == None:
            print('Failed to get sandbox name', file=sys.stderr)
            sys.exit(1)


# Metrics scraped from buldfarm server
class BuildfarmServerMetrics:
    queue_size = 0
    worker_pool_size = 0 

# Metrics scraped from buildfarm worker
class BuildfarmWorkerMetrics:
    worker_name = ""
    execution_slot_usage = 0


def main():
    load_config()
    log("scrape_period: {}".format(scrape_period))
    log("delay_of_retry: {}".format(delay_of_retry))
    log("trending_duration: {}".format(trending_duration))
    log("min_worker: {}".format(min_worker))
    log("max_worker: {}".format(max_worker))
    g = Global()

    while True:
        err = scale(g)
        if err != None:
            log_error(str(err))
            time.sleep(delay_of_retry)
            continue
        time.sleep(scrape_period)
    return

def load_config():
    global min_worker
    global max_worker
    global scrape_period
    global delay_of_retry
    global trending_duration

    min_worker = load_env_or_default("BF_MIN_WORKER",min_worker)
    if min_worker < 1:
        min_worker = 1
    
    max_worker = load_env_or_default("BF_MAX_WORKER",max_worker)
    if max_worker < min_worker:
        max_worker = min_worker
    
    scrape_period = load_env_or_default("BF_SCRAPE_PERIOD",scrape_period)
    if scrape_period <=0:
        scrape_period = 1
    
    delay_of_retry = load_env_or_default("BF_SCALE_DELAY_OF_RETRY",delay_of_retry)
    if delay_of_retry < 0:
        delay_of_retry = 0
    
    trending_duration = load_env_or_default("BF_SCALE_TRENDING_DURATION",trending_duration)

def load_env_or_default(key,default):
    v = os.getenv(key)
    if v == None:
        return default
    return int(v)
    


def scale(g):
    server_metrics,err = scrape_buildfarm_server_metrics()
    if err != None:
        log_error("failed to scrape buildfarm server metrics: " + str(err))
        return False
    now = datetime.datetime.now()
    log("{} , queue size is {}".format(now,server_metrics.queue_size))
    # Update scale trending. 
    # If actions queue size greater than 0, update the scale trending to scale up trending(add worker). 
    # Otherwise, update the scale trending to scale down trending(remove worker).
    if server_metrics.queue_size > 0:
        if not g.is_scale_up_trending:
            log("queue_szie is {}, switched to scale up trending".format(server_metrics.queue_size))
            g.is_scale_up_trending = True
            g.trending_start_time = now
    else:
        if g.is_scale_up_trending:
            log("queue_szie is 0, switched to scale down trending")
            g.is_scale_up_trending = False
            g.trending_start_time = now
    
    # Update expected worker number when the trend has lasted for a certain period of time
    if (now - g.trending_start_time).total_seconds() >= trending_duration:
        # The server_metrics.worker_pool_size is the number of worker that has connected to the buildfarm server
        # Only scale up/down when the previous scaled worker has really connected to buildfarm server.
        if g.is_scale_up_trending and g.expected_worker_num <= server_metrics.worker_pool_size:
            g.expected_worker_num += 1
        if not g.is_scale_up_trending and g.expected_worker_num >= server_metrics.worker_pool_size:
            g.expected_worker_num -= 1

        # Limit min and max worker.
        if g.expected_worker_num > max_worker:
            g.expected_worker_num = max_worker
        if g.expected_worker_num <  min_worker:
            g.expected_worker_num = min_worker

    if g.expected_worker_num > server_metrics.worker_pool_size:
        return scale_up(g,server_metrics)

    if g.expected_worker_num < server_metrics.worker_pool_size:
        return scale_down(g, server_metrics)

    return None


# Scale up the worker by updating the sandbox definition.
def scale_up(g,server_metrics):
    template,err = sandbox_template(g)
    if err != None:
        return err
    workers_in_template = workers_defined_in_template(template)
    scale_num = g.expected_worker_num - len(workers_in_template)  
    if scale_num <=  0:
        return
    log("scaling up to {} workers, need to add {} worker".format(g.expected_worker_num, scale_num))
    for i in range(0,scale_num):
        alloc_worker_to_template(template)
    log("updating sandbox")
    result = subprocess.run(["cs","sandbox","edit","--force","--from","-"],input=json.dumps(template),text=True,stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        log_error("failed to scale up worker: {}".format(result.stderr))
        return result.stderr
    log("scaled worker num to {}".format(g.expected_worker_num))
    return None


# Scale down the workers by updating the sandbox definition.
def scale_down(g,server_metrics):
    template,err = sandbox_template(g)
    if err != None:
        return log_error(err)
    workers_in_template = workers_defined_in_template(template)

    scale_num =  len(workers_in_template)  - g.expected_worker_num 
    if scale_num <= 0:
        return

    log("scaling down to {} workers, need to remove {} worker".format(g.expected_worker_num, scale_num))
    
    metrics =[]
    has_idled_worker = False
    for i in range(0,scale_num):
        for worker in workers_in_template:
            worker_metric,err = scrape_buildfarm_worker_metrics(worker["name"])
            if err == None:
                metrics.append(worker_metric)
        idle_workers =list(filter(lambda x: x.execution_slot_usage ==0, metrics))
        if len(idle_workers) == 0:
            continue
        has_idled_worker = True
        template["containers"] = list(filter(lambda container : container["name"] != idle_workers[0].worker_name,template["containers"] ))
    if not has_idled_worker:
        log("All workers are busy, do not remove now")
        return None
    log("updating sandbox")
    result = subprocess.run(["cs","sandbox","edit","--force","--from","-"],input=json.dumps(template),text=True, stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        log_error("failed to scale down worker: {}".result.stderr)
        return result.stderr
    log("scaled down worker to {}".format(len(workers_defined_in_template(template))))
    return None

# Get sandbox definition
def sandbox_template(g):
    result = subprocess.run(["cs","sandbox","show",g.sandbox_name,"-d","-o","json"],capture_output=True, text=True)
    if result.returncode != 0 :
        return None,result.stderr
    data,err = parse_json(result.stdout)
    return data,err
 

# Get buildfarm-workers containers in sandbox definition
def workers_defined_in_template(template):
    return list(filter(lambda container: container["name"].startswith("bf-worker"), template["containers"]))

# Add one buildfarm-worker container to definition
def alloc_worker_to_template(template):
    workers = workers_defined_in_template(template)
    for ii in range (0,max_worker):
        worker_name = "bf-worker"+str(ii)
        if not has_bf_worker(workers,worker_name):
            worker = workers[0].copy()
            worker["name"] = worker_name
            template["containers"].append(worker)
            return


def has_bf_worker(workers,name):
    for worker in workers:
        if worker["name"] == name:
            return True

    return False


# Scrape the metrics of buildfarm server.
def scrape_buildfarm_server_metrics():
    server_metrics = BuildfarmServerMetrics()
    try:
        response = requests.get(bf_server_metrics_url)
        response.close()
        if response.status_code != 200:
            return None,response.text
    except Exception as error:
        return None,err
    metrics = parse_metrics(response.text)
    queue_size_key="queue_size"
    worker_pool_size_key = "worker_pool_size"

    if not queue_size_key in metrics:
        return None, "metric {} not found".format(queue_size_key)
    queue_size = 0
    for k,v in metrics[queue_size_key].items():
        server_metrics.queue_size += int(float(v))
    if not worker_pool_size_key in metrics:
        return None, "metric {} not found".format(worker_pool_size_key)

    server_metrics.worker_pool_size = int(float(metrics[worker_pool_size_key]))
    return server_metrics,None

def parse_metrics(content):
    buf = io.StringIO(content)
    metrics = {}
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
        except Exception as error:
            continue
        key = line[:index]
        value = line[index+1:]
        try:
            label_index = key.index("{")
        except Exception as error:
            metrics[key] = value
            continue
        label = key[label_index:]
        group=key[:label_index]
        if group in metrics:
            metrics[group][label] = value
        else:
            metrics[group]={label:value}
    return metrics


# Scrape the metrics of a buildfarm worker.
def scrape_buildfarm_worker_metrics(worker_name):
    url = "http://{}:9090/metric".format(worker_name)
    worker_metrics = BuildfarmWorkerMetrics() 
    worker_metrics.worker_name = worker_name

    result = subprocess.run(["prom2json",url],capture_output=True, text=True)
    try:
        response = requests.get(url)
        response.close()
        if response.status_code != 200:
            return None,response.text
    except Exception as error:
        return None,err

    metrics = parse_metrics(response.text)
    slot_usage_key = "execution_slot_usage"
    if not slot_usage_key in metrics:
        return None, "metrics {} not found".format(slot_usage_key)
    worker_metrics.execution_slot_usage = int(float(metric[slot_usage_key]))


    if result.returncode != 0 :
        return None,result.stderr
    data,err = parse_json(result.stdout)
    if err != None:
        return None,err
    metrics.execution_slot_usage= functools.reduce(
        lambda a,b: a+b,
        map(
            lambda metric: functools.reduce(lambda a,b:a+b,map(lambda x: int(x["value"]),metric["metrics"])),
            filter(lambda obj: obj["name"] == "execution_slot_usage", data),
        ),
    )
    return metrics,None

def log_error(msg):
    if isinstance(msg, str):
        print("Error: " + msg, file=sys.stderr)
    else:
        print("Error: " + str(msg), file=sys.stderr)

def log(msg):
    print(msg)

def fatal(msg):
    log_error(msg)
    sys.exit(1)

def parse_json(json_string):
    try:
        data = json.loads(json_string)
        return data,None
    except Exception as error:
        return None,error


if __name__ == "__main__":
    main()
