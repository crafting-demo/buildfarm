#!/bin/bash

set -e 

function install_clang_16(){
	sudo apt update
	sudo apt install -y lsb-release wget software-properties-common gnupg curl
	curl -o /tmp/llvm.sh -L  https://apt.llvm.org/llvm.sh
	sudo bash /tmp/llvm.sh 16
}

# Install clang which is needed by preflight check of `./configure`. We don't need the clang in local when we actually build it remote (But need in remote)
[[ -e "/usr/lib/llvm-16/bin/clang" ]] || install_clang_16

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
