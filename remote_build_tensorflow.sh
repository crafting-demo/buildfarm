#!/bin/bash

set -e 

CONTAINER_NAME="bf-worker-bash"

if [[ $1 == "" ]];then
	docker port $CONTAINER_NAME || docker run -dt --network host --name $CONTAINER_NAME -v /home/owner/tensorflow:/tensorflow koitown/buildfarm-worker-bash:v2.5.0
	docker exec -it $CONTAINER_NAME bash -c "cd /tensorflow && bash $0"
	
else
	echo "Configuring tensorflow building"
	CLANG_COMPILER_PATH=/usr/lib/llvm-16/bin/clang \
		TF_NEED_CLANG=true \
		PYTHON_BIN_PATH=/usr/bin/python3 \
		USE_DEFAULT_PYTHON_LIB_PATH=1  \
		TF_NEED_CUDA=false \
		TF_NEED_ROCM=false \
		CC_OPT_FLAGS=-Wno-sign-compare \
		TF_SET_ANDROID_WORKSPACE=false \
		./configure
	echo "Configure successfully"

	echo "Start building"
	bazel build --verbose_failures --jobs=30 --spawn_strategy=remote --remote_executor=grpc://localhost:8980 //tensorflow/tools/pip_package:build_pip_package

fi

