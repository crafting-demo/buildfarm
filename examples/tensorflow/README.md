# Experience buildfarm auto scaliing by building tensorflow

This example will verify the feature of auto scaling buildfarm by building tensorflow.

# Operation Steps
* Ensure the auto-scaling script is running
```
ps -ef | grep "sandbox/auto_scale.py"
# Run it if not running yet
python3 ~/buildfarm/.sandbox/auto_scale.py
```
* Clone tensorflow repo and update buildfarm worker container image. For building tensorflow we need to ensure the buildfarm worker image containing the required tools and pacakges(clang-11 etc...).
```
./init_tensorflow_build.sh
```
* Building tensorflow with buildfarm. The number of buildfarm worker will be auto scaled during building.
```
./remote_build_tensorflow.sh
```