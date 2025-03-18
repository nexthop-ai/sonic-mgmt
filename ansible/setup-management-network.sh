#!/bin/bash
if [[ $(id -u) -ne 0 ]]; then
    echo "Root privilege required"
    exit
fi

function show_help_and_exit()
{
    echo "Usage ${SCRIPT} [options]"
    echo "    options with (*) must be provided"
    echo "    -h -?                  : get this help"
    echo "    -d                     : Delete existed bridge"
    echo "    -b <bridge_name>       : Bridge name (default: br1)"
    echo "    -i <ip_address>        : Bridge IPv4 address with prefix (default: 10.250.0.1/24)"
    echo "    -i6 <ipv6_address>     : Bridge IPv6 address with prefix (default: fec0::1/64)"

    exit $1
}

DEL_EXISTED_BRIDGE=false
BRIDGE_NAME="br1"
BRIDGE_IP="10.250.0.1/24"
IPV6_PREFIX="fec0::1/64"

while getopts "h?db:i:i6:" opt; do
    case ${opt} in
        h|\? )
            show_help_and_exit 0
            ;;
        d)
            DEL_EXISTED_BRIDGE=true
            ;;
        b)
            BRIDGE_NAME="${OPTARG}"
            ;;
        i)
            BRIDGE_IP="${OPTARG}"
            ;;
        i6)
            IPV6_PREFIX="${OPTARG}"
            ;;
    esac
done

echo "Using bridge name: ${BRIDGE_NAME}"
echo "Using bridge IPv4: ${BRIDGE_IP}"
echo "Using bridge IPv6: ${IPV6_PREFIX}"
echo

echo "Refreshing apt package lists..."
apt-get update
echo

echo "STEP 1: Checking for j2cli package..."
if ! command -v j2; then
    echo "j2cli not found, installing j2cli"
    cmd="install --user j2cli==0.3.10"
    if ! command -v pip &> /dev/null; then
        pip3 $cmd
    else
        pip $cmd
    fi
fi
echo

echo "STEP 2: Checking for bridge-utils package..."
if ! command -v brctl; then
    echo "brctl not found, installing bridge-utils"
    apt-get install -y bridge-utils
fi
echo

echo "STEP 3: Checking for net-tools package..."
if ! command -v ifconfig; then
    echo "ifconfig not found, install net-tools"
    apt-get install -y net-tools
fi
echo

echo "STEP 4: Checking for ethtool package..."
if ! command -v ethtool; then
    echo "ethtool not found, install ethtool"
    apt-get install -y ethtool
fi
echo

echo "STEP 5: Delete existed ${BRIDGE_NAME}..."
if [ "$DEL_EXISTED_BRIDGE" = true ] && ifconfig ${BRIDGE_NAME} >/dev/null 2>&1; then
    echo "${BRIDGE_NAME} exists, remove it."
    ifconfig ${BRIDGE_NAME} down
    brctl delbr ${BRIDGE_NAME}
else
    echo "Not delete existed bridge or ${BRIDGE_NAME} not exists, skipping..."
fi
echo

echo "STEP 6: Checking if bridge ${BRIDGE_NAME} already exists..."
if ! ifconfig ${BRIDGE_NAME}; then
    echo "${BRIDGE_NAME} not found, creating bridge network"
    brctl addbr ${BRIDGE_NAME}
    brctl show ${BRIDGE_NAME}
else
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo
    echo "  ${BRIDGE_NAME} exists, possibly lab server, are you sure you want to continue?"
    echo
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo
    echo
    echo "Please double check and manually configure IP for ${BRIDGE_NAME} to avoid breaking lab server connectivity"
    exit 0
fi
echo

echo "STEP 7: Configuring ${BRIDGE_NAME} interface..."
echo "Assigning ${BRIDGE_IP} to ${BRIDGE_NAME}"
ifconfig ${BRIDGE_NAME} ${BRIDGE_IP}
echo "Assigning ${IPV6_PREFIX} to ${BRIDGE_NAME}"
ifconfig ${BRIDGE_NAME} inet6 add ${IPV6_PREFIX}
echo "Bringing up ${BRIDGE_NAME}"
ifconfig ${BRIDGE_NAME} up
echo

echo "COMPLETE. Bridge info:"
echo
brctl show ${BRIDGE_NAME}
echo
ifconfig ${BRIDGE_NAME}
