IMAGE_NAME=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep "librelane" | head -n 1)

if [ -z "$IMAGE_NAME" ]; then
    echo "Error: Librelane image not found!"
    exit 1
fi

docker run --rm -v $(pwd):/openlane -w /openlane "$IMAGE_NAME" sta power_vcd.tcl

