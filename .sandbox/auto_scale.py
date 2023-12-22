#!/usr/bin/python3

import time
import sys
import os
import subprocess
import json
import datetime
import functools

bf_server_metrics_url="http://bf-server:9090/metrics"
scale_duration = 20
min_worker = 1
max_worker = 4

class Global:
    sandbox_name = ""
    queue_empty_start_time = 0
    expected_worker_num = 1
    is_scale_up_trending = True
    trending_start_time = None
    def __init__(self):
        self.sandbox_name = os.getenv("SANDBOX_NAME")
        if self.sandbox_name == "" or self.sandbox_name == None:
            print('Failed to get sandbox name', file=sys.stderr)
            sys.exit(1)

class BuildfarmServerMetrics:
    queue_size = 0
    worker_pool_size = 0 

def main():
    install_promtojson_cli()
    g = Global()

    while True:
        err = scale(g)
        if err != None:
            time.sleep(2)
            continue
        time.sleep(5)
    return

def scale(g):
    server_metrics,err = scrape_buildfarm_server_metrics()
    if err != None:
        log.error("failed to scrape buildfarm server metrics: " + str(err))
        return False
    now = datetime.datetime.now()
    if server_metrics.queue_size > 0:
        if not g.is_scale_up_trending:
            g.is_scale_up_trending = true
            g.trending_start_time = now
    else:
        if g.is_scale_up_trending:
            g.is_scale_up_trending = false
            g.trending_start_time = now
    
    if (now - g.trending_start_time).total_seconds() >= scale_duration:
        if g.is_scale_up_trending and g.expected_worker_num <= server_metrics.worker_pool_size:
            g.expected_worker_num += 1
        if not g.is_scale_up_trending and g.expected_worker_num >= server_metrics.worker_pool_size:
            g.expected_worker_num -= 1

        if g.expected_worker_num > max_worker:
            g.expected_worker_num = max_worker
        if g.expected_worker_num <  min_worker:
            g.expected_worker_num = min_worker

    if g.expected_worker_num > server_metrics.worker_pool_size:
        return scale_up(g,server_metrics)

    if g.expected_worker_num < server_metrics.worker_pool_size:
        return scale_down(g, server_metrics)

    return None


def scale_up(g,server_metrics):
    template,err = sandbox_template()
    if err != None:
        return err
    workers_in_template,err = workers_defined_in_template(template)
    if err != None:
        return err
    scale_num = g.expected_worker_num - len(workers_in_template)  
    if scale_num == 0:
        return
    for i in range(0,scale_num):
        alloc_worker_to_template(template)
    result = subprocess.run("cs sandbox edit --from -",input=json.dumps(template),capture_output=True, text=True)
    if result.returncode != 0:
        return result.stderr
    return None


def scale_down(g,server_metrics):
    # TODO
    return

def sandbox_template():
    result = subprocess.run(["cs","sandbox","show",g.sandbox_name,"-d","-o","json"],capture_output=True, text=True)
    if result.returncode != 0 :
        return None,result.stderr
    data,err = parse_json(result.stdout)
    return data,err


def workers_defined_in_template(template):
    return list(filter(lambda x: x.name.startWith("bf-worker"), template["containers"])), None

def alloc_worker_to_template(template):
    for ii in range (0,max_worker):
        worker_name = "bf-worker"+str(ii)
        if not template_has_bf_worker(template,worker_name):
            worker = template["containers"][0].copy()
            worker["name"] = worker_name
            template["containers"].append(worker)


def template_has_bf_worker(template,name):
    for worker in workers_defined_in_template(template):
        if worker.name == name:
            return true

    return false


def scrape_buildfarm_server_metrics():
    server_metrics = BuildfarmServerMetrics()
    result = subprocess.run(["prom2json",bf_server_metrics_url],capture_output=True, text=True)
    if result.returncode != 0:
        return None, result.stderr
    data,err = parse_json(result.stdout)
    if err != None:
        return None,err
    server_metrics.queue_size = functools.reduce(
        lambda a,b: a+b,
        map(
            lambda metric: functools.reduce(lambda a,b:a+b,map(lambda x: int(x["value"]),metric["metrics"])),
            filter(lambda obj: obj["name"] == "queue_size", data),
        ),
    )
    server_metrics.worker_pool_size = functools.reduce(
        lambda a,b: a+b,
        map(
            lambda metric: functools.reduce(lambda a,b:a+b,map(lambda x: int(x["value"]),metric["metrics"])),
            filter(lambda obj: obj["name"] == "worker_pool_size", data),
        ),
    )
    print(server_metrics.worker_pool_size)
    
def install_promtojson_cli():
    result = subprocess.run(["which","prom2json"])
    if result.returncode == 0:
        return
    tmp_dir = "/tmp/sandbox_prom2json"
    download_file_path = tmp_dir + "/protm2json.tar.gz"
    extract_dir_path = tmp_dir + "/uncompressed"
    if subprocess.run(["mkdir","-p",extract_dir_path]).returncode != 0:
        fatal("failed to create directory " + extract_dir_path)
    download_url = "https://github.com/prometheus/prom2json/releases/download/v1.3.3/prom2json-1.3.3.linux-amd64.tar.gz"
    log("Downloading protm2json from " + download_url)
    download_result = subprocess.run(["curl","-Ss","-L","-o",download_file_path, download_url])
    if download_result.returncode != 0:
        fatal("failed to download protm2json")
    tar_result = subprocess.run(["tar","-xf",download_file_path,"-C",extract_dir_path,"--strip-components=1"])
    if tar_result.returncode != 0:
        fatal("failed to uncompressed prom2json")
    if subprocess.run(["sudo","cp",extract_dir_path+"/prom2json", "/usr/bin/"]).returncode != 0:
        fatal("failed to install prom2json")
    log("protm2json installed")






def log_error(msg):
    print("Error: " + msg, file=sys.stderr)

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
