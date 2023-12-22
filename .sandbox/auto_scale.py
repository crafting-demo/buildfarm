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
    worker_num_in_tempalte,err = worker_num_defined_in_template()
    if err != None:
        return err
    if worker_num_in_tempalte == g.expected_worker_num:
        return None
    # TODO update template

    return

def scale_down(g,server_metrics):
    # TODO
    return

def worker_num_defined_in_template():
    # TODO
    return

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
    

def scale_down_if_need():
    pass

def scrape_queue_size():
    result = subprocess.run(["prom2json",bf_server_prom_url],capture_output=True, text=True)
    if result.returncode != 0:
        return None,result.stderr
    data,err = parse_json(result.stdout)
    if err != None:
        return None, str(err)
    queue_size = 0
    for obj in data:
        if obj["name"] != "queue_name":
            continue
        for metric in obj.metrics:
            queue_size += int(meric.value)
        return queue_size,None
    return 0,None


    

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
