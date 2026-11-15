#!/usr/bin/env bash
# Installs Apache JMeter and the JDBC driver of one engine into local/tools (checksums verified).
#   jmeter/install.sh [engine]      engine defaults to mysql; drivers are listed in jmeter/drivers/<engine>.env
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="${ROOT}/local/tools"
JMETER_VERSION=5.6.3
JMETER_SHA512=5978a1a35edb5a7d428e270564ff49d2b1b257a65e17a759d259a9283fc17093e522fe46f474a043864aea6910683486340706d745fcdf3db1505fd71e689083
ENGINE="${1:-mysql}"

[[ "$ENGINE" =~ ^[a-z0-9_]+$ ]] || { echo "invalid engine name: $ENGINE"; exit 1; }
DRIVER_FILE="${ROOT}/jmeter/drivers/${ENGINE}.env"
[[ -f "$DRIVER_FILE" ]] || { echo "no JDBC driver defined for engine '${ENGINE}' (${DRIVER_FILE})"; exit 1; }
# shellcheck disable=SC1090
source "$DRIVER_FILE"

command -v java >/dev/null || { echo "Java 8+ is required (java not found)"; exit 1; }
mkdir -p "$TOOLS"
cd "$TOOLS"

if [[ ! -x "apache-jmeter-${JMETER_VERSION}/bin/jmeter" ]]; then
  curl -fsSLO "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz"
  echo "${JMETER_SHA512}  apache-jmeter-${JMETER_VERSION}.tgz" | shasum -a 512 -c -
  tar xzf "apache-jmeter-${JMETER_VERSION}.tgz"
  rm "apache-jmeter-${JMETER_VERSION}.tgz"
fi

jar="apache-jmeter-${JMETER_VERSION}/lib/${DRIVER_ARTIFACT}-${DRIVER_VERSION}.jar"
if [[ ! -f "$jar" ]]; then
  base="https://repo1.maven.org/maven2/${DRIVER_GROUP}/${DRIVER_ARTIFACT}/${DRIVER_VERSION}/${DRIVER_ARTIFACT}-${DRIVER_VERSION}.jar"
  curl -fsSL -o "$jar" "$base"
  echo "$(curl -fsSL "${base}.sha1")  ${jar}" | shasum -a 1 -c -
fi
ln -sfn "apache-jmeter-${JMETER_VERSION}" jmeter
echo "JMeter ready: ${TOOLS}/jmeter/bin/jmeter (JDBC driver: ${DRIVER_ARTIFACT} ${DRIVER_VERSION})"
