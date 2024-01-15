#!/bin/bash
# This script will clone the tensorflow repo and set the building enviroment for tensorflow.

set -e

[[ -e "/home/owner/tensorflow" ]] || git clone --depth=1 --branch=v2.15.0 https://github.com/tensorflow/tensorflow.git /home/owner/tensorflow

# Change bf-worker image to koitown/bf-worker:v2.5.0. When remote building tensorflow, we need clang-16 in the bf-worker container.
echo "Updating sandbox bf-worker image"
template_json=$(cs sandbox show $SANDBOX_NAME -d -o json)

c=`cat <<EOF
import sys
import json
import subprocess

template_str = sys.argv[1]
template = json.loads(template_str)

for container in template["containers"]:
	if container["name"].startswith("bf-worker"):
		container["image"]= "koitown/buildfarm-worker:v2.5.0"

result = subprocess.run(["cs","sandbox","edit","--force","--from","-"],input=json.dumps(template),text=True)
if result.returncode !=0 :
	print("update temlate error: ",result.stderr)
	sys.exit(1)
EOF`

python3 -c "$c" "$template_json"
cp remote_build_tensorflow.sh /home/owner/tensorflow/remote_build_tensorflow.sh

echo "Successful. Now you could 'cd ~/tensorflow && remote_build_tensorflow.sh' . The worker nodes will auto scale when compiling tensorflow"
