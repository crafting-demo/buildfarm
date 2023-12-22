#!/usr/bin/python3

import time
import sys
import os
import subprocess
import json
import datetime

bf_server_prom_url="http://bf-server:9090/metrics"

class Global:
    sandbox_name = ""
    queue_empty_start_time = 0
    def __init__(self):
        self.sandbox_name = os.getenv("SANDBOX_NAME")
        if self.sandbox_name == "" or self.sandbox_name == None:
            print('Failed to get sandbox name', file=sys.stderr)
            sys.exit(1)

def main():
    install_promtojson_cli()
    g = Global()

    while True:
        err = scale_worker(g)
        if err != None:
            time.sleep(2)
            continue
        time.sleep(5)
    return

def scale_worker(g):
    # Query 
    queue_size,err = scrape_queue_size()
    if err != None:
        log_error(err)
        return False

    scale_downed,err = scale_down_if_need(g)
    if err != None:
        log_error(err)
        return False
    if scale_downed:
        return

    scale_upped,err = scale_up_if_need(g)
    if err != None:
        log_error(err)
        return False
    return None

    #result = subprocess.run(["cs","sandbox","show",g.sandbox_name,"-d","-o","json"],capture_output=True, text=True)
    #if result.returncode != 0 :
    #    log_error("failed to get sandbox information: " + result.stderr)
    #    return False
    #sandbox_def,error = parse_json(result.stdout)
    #if error != None:
    #    log_error("failed to parse sandbox definition from json: "+ str(error))
    #    return False
    #return None

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
