"""Run: YQ=/path/to/mikefarah/yq python3 -m unittest discover -s services/adguard -v."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("busybox"), "BusyBox required")
class DNSProbeTests(unittest.TestCase):
    def probe(self, rcode=0, delay=0, reply=True):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver:
            resolver.bind(("127.0.0.1", 0))
            resolver.settimeout(3)

            def respond():
                try:
                    packet, peer = resolver.recvfrom(4096)
                except TimeoutError:
                    return
                time.sleep(delay)
                resolver.sendto(packet[:2] + bytes((0x81, 0x80 | rcode)) + packet[4:], peer)

            thread = None
            if reply:
                thread = threading.Thread(target=respond)
                thread.start()
            try:
                return subprocess.run(
                    ["sh", str(ROOT / "check-dns")],
                    env={**os.environ, "DNS_PROBE_PORT": str(resolver.getsockname()[1])},
                    capture_output=True, timeout=20,
                ).returncode
            finally:
                if thread is not None:
                    thread.join()

    def test_requires_successful_response(self):
        for name, rcode in (("NOERROR", 0), ("SERVFAIL", 2), ("REFUSED", 5), ("NXDOMAIN", 3)):
            with self.subTest(status=name):
                self.assertEqual(self.probe(rcode) == 0, rcode == 0)

    def test_allows_delayed_fallback_response(self):
        self.assertEqual(self.probe(delay=5.5), 0)

    def test_timeout_fails(self):
        self.assertNotEqual(self.probe(reply=False), 0)


class DependencyWaitTests(unittest.TestCase):
    def run_wait(self, getent, seconds=2, world="1", expire_after_poll=False):
        with tempfile.TemporaryDirectory() as tmp:
            mock = Path(tmp, "getent")
            mock.write_text("#!/usr/bin/env bash\n" + getent)
            mock.chmod(0o755)
            setup = "sleep() { SECONDS=$deadline; }; " if expire_after_poll else ""
            started = time.monotonic()
            result = subprocess.run(
                ["bash", "-c", setup + 'source "$1"; wait_for_dependencies "$2"; '
                 'printf "%s|%s|%s" "$AZ_LOCAL_HOST" "$AZ_WORLD_HOST" "$COREDNS_HOST"',
                 "test", str(ROOT / "dependencies.sh"), str(seconds)],
                env={**os.environ, "PATH": tmp + os.pathsep + os.environ["PATH"],
                     "AZ_WORLD_ENABLED": world, "STATE_FILE": str(Path(tmp, "state"))},
                capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result, time.monotonic() - started

    def test_all_dependencies_ready(self):
        result, _ = self.run_wait('echo "172.20.0.4 $2"\n')
        self.assertEqual(result.stdout, "172.20.0.4|172.20.0.4|172.20.0.4")
        self.assertNotIn("expired", result.stderr)

    def test_disabled_world_is_not_required(self):
        result, _ = self.run_wait('[[ "$2" != az-world.antizapret ]] || exit 1\n'
                                  'echo "172.20.0.4 $2"\n', world="0")
        self.assertEqual(result.stdout, "172.20.0.4||172.20.0.4")
        self.assertNotIn("expired", result.stderr)

    def test_missing_dependency_keeps_available_addresses(self):
        # Expire between polls so this checks the retained addresses. The stuck
        # resolver test separately exercises the real one-second deadline.
        result, _ = self.run_wait('[[ "$2" != az-world.antizapret ]] || exit 2\n'
                                  'echo "172.20.0.4 $2"\n', seconds=4, expire_after_poll=True)
        self.assertEqual(result.stdout, "172.20.0.4||172.20.0.4")
        self.assertIn("expired", result.stderr)

    def test_stuck_resolution_is_bounded(self):
        result, elapsed = self.run_wait("exec sleep 5\n", seconds=1)
        self.assertEqual(result.stdout, "||")
        self.assertIn("expired", result.stderr)
        self.assertLess(elapsed, 2.5)

    def test_recovers_during_wait(self):
        result, _ = self.run_wait('if [[ ! -f "$STATE_FILE" ]]; then\n'
                                  '  touch "$STATE_FILE"; exit 2\nfi\n'
                                  'echo "172.20.0.4 $2"\n', seconds=4)
        self.assertEqual(result.stdout, "172.20.0.4|172.20.0.4|172.20.0.4")
        self.assertNotIn("expired", result.stderr)


@unittest.skipUnless(os.environ.get("YQ") or shutil.which("yq"), "Mike Farah yq required")
class ConfigMigrationTests(unittest.TestCase):
    def migrate(self, config, **settings):
        script = (ROOT / "entrypoint.sh").read_text()
        expression = "\n    (.dns.upstream_dns" + script.split(
            "yq -i '\n    (.dns.upstream_dns", 1
        )[1].split("' /opt/adguardhome/conf/AdGuardHome.yaml", 1)[0]
        result = subprocess.run(
            [os.environ.get("YQ", "yq"), "-o=json", expression], input=json.dumps(config),
            env={**os.environ, "AZ_WORLD_CLIENT_IDS": '["az-world"]', **settings},
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def config(self, fallback=None):
        config = json.loads(subprocess.check_output(
            [os.environ.get("YQ", "yq"), "-o=json", str(ROOT / "AdGuardHome.yaml")], text=True
        ))
        if fallback is None:
            config["dns"].pop("fallback_dns")
        else:
            config["dns"]["fallback_dns"] = fallback
        config["dns"]["upstream_timeout"] = "1s"
        return config

    def test_migrates_only_bundled_routing_rules(self):
        defaults = [
            '||*^$dnsrewrite=SERVFAIL,client=az-local',
            '||*^$dnsrewrite=SERVFAIL,client=az-world',
        ]
        custom = [
            '||*^$dnsrewrite=SERVFAIL,client=az-local,important',
            '||example.test^$dnsrewrite=SERVFAIL,client=az-world',
        ]
        url = 'http://az-local.antizapret/list/?client=az-local&dnsrewrite=SERVFAIL'
        config = self.config()
        config["user_rules"] = defaults + custom
        config["filters"] = [{"url": url}]

        migrated = self.migrate(config)
        self.assertEqual(migrated["user_rules"], [rule.replace("SERVFAIL", "REFUSED") for rule in defaults] + custom)
        self.assertEqual(migrated["filters"], config["filters"])
        self.assertEqual(self.migrate(migrated), migrated)

    def test_existing_install_gets_fallback_and_timeout(self):
        for fallback in (None, []):
            with self.subTest(fallback=fallback):
                config = self.migrate(self.config(fallback))
                self.assertEqual(config["dns"]["upstream_timeout"], "5s")
                self.assertEqual(set(config["dns"]["fallback_dns"]), {
                    "https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query",
                    "[/*.antizapret/]127.0.0.11",
                })
                self.assertTrue(all("" not in c["ids"] for c in config["clients"]["persistent"]))

    def test_custom_fallback_preserved_and_migration_idempotent(self):
        config = self.migrate(self.config(["https://dns.example/dns-query"]))
        self.assertEqual(set(config["dns"]["fallback_dns"]), {
            "https://dns.example/dns-query", "[/*.antizapret/]127.0.0.11",
        })
        self.assertEqual(self.migrate(config), config)


class DNSReloadTests(unittest.TestCase):
    def test_healthcheck_repairs_dns_without_refreshing_unchanged_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / ".inited").touch()
            (work / ".config_md5").write_text("abc abc\n")
            (work / ".dns_dependencies").write_text(
                '{"local":"10.0.0.1","world":"10.0.0.9","coredns":"10.0.0.3"}\n'
            )
            curl = work / "curl"
            curl.write_text('''#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in http://*) url=$arg ;; esac
done
echo "$url" >> "$API_LOG"
case "$url" in
    */control/clients)
        echo '{"clients":[{"name":"az-local","ids":["az-local","10.0.0.1"]},{"name":"az-world","ids":["az-world","10.0.0.2"]},{"name":"coredns","ids":["coredns","10.0.0.3"]}]}' ;;
    */control/dns_info) echo '{"upstream_dns":["coredns.antizapret"]}' ;;
    */control/dns_config) echo OK ;;
    */config-md5/) echo abc ;;
    *) exit 22 ;;
esac
''')
            getent = work / "getent"
            getent.write_text('''#!/usr/bin/env bash
case "$2" in
    az-local.antizapret) echo '10.0.0.1 az-local' ;;
    az-world.antizapret) echo '10.0.0.2 az-world' ;;
    coredns.antizapret) echo '10.0.0.3 coredns' ;;
esac
''')
            curl.chmod(0o755)
            getent.chmod(0o755)
            script = (ROOT / "refresh-state.sh").read_text().replace(
                "/dev/shm/", tmp + "/"
            ).replace("/root/dns-reload.sh", str(ROOT / "dns-reload.sh"))
            env = {**os.environ, "PATH": tmp + os.pathsep + os.environ["PATH"],
                   "API_LOG": str(work / "requests"), "AZ_WORLD_ENABLED": "1"}
            for _ in range(2):
                result = subprocess.run(["bash", "-c", script], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            requests = (work / "requests").read_text().splitlines()
            reconfig = "http://127.0.0.1:3000/control/dns_config"
            self.assertEqual(requests.count(reconfig), 1)
            self.assertLess(requests.index(reconfig), requests.index("http://az-local.antizapret/config-md5/"))
            self.assertNotIn("http://127.0.0.1:3000/control/filtering/refresh", requests)
            self.assertEqual((work / ".config_md5").read_text(), "abc abc\n")
            self.assertEqual(json.loads((work / ".dns_dependencies").read_text())["world"], "10.0.0.2")

    def test_dependency_events_reload_once_each(self):
        self.run_scenario('''
dependency_snapshot 10.0.0.1 10.0.0.2 10.0.0.3 > "$DNS_DEPENDENCY_STATE_FILE"
reload_dns_on_dependency_change 10.0.0.1 10.0.0.2 10.0.0.3
[[ ! -s "$API_LOG" ]]
# Disappearance, same-address recovery, world IP change, CoreDNS IP change.
for addresses in '10.0.0.1 none 10.0.0.3' '10.0.0.1 10.0.0.2 10.0.0.3' \
                 '10.0.0.1 10.0.0.4 10.0.0.3' '10.0.0.1 10.0.0.4 10.0.0.5'; do
    read -r local_ip world_ip core_ip <<< "$addresses"
    [[ "$world_ip" != none ]] || world_ip=''
    echo old-hash > "$DNS_CONFIG_MD5_FILE"
    reload_dns_on_dependency_change "$local_ip" "$world_ip" "$core_ip"
    [[ "$(cat "$DNS_CONFIG_MD5_FILE")" == old-hash ]]
    before=$(wc -l < "$API_LOG")
    reload_dns_on_dependency_change "$local_ip" "$world_ip" "$core_ip"
    [[ "$(wc -l < "$API_LOG")" == "$before" ]]
done
[[ "$(wc -l < "$API_LOG" | tr -d ' ')" == 8 ]]
''')

    def test_failed_reconfiguration_is_retried(self):
        self.run_scenario('''
dependency_snapshot 10.0.0.1 '' 10.0.0.3 > "$DNS_DEPENDENCY_STATE_FILE"
cp "$DNS_DEPENDENCY_STATE_FILE" "$TEST_DIR/previous"
echo old-hash > "$DNS_CONFIG_MD5_FILE"
FAIL_POST=1
if reload_dns_on_dependency_change 10.0.0.2 '' 10.0.0.3; then exit 1; fi
cmp "$DNS_DEPENDENCY_STATE_FILE" "$TEST_DIR/previous"
[[ "$(cat "$DNS_CONFIG_MD5_FILE")" == old-hash ]]
FAIL_POST=0
reload_dns_on_dependency_change 10.0.0.2 '' 10.0.0.3
[[ "$(cat "$DNS_CONFIG_MD5_FILE")" == old-hash ]]
[[ "$(wc -l < "$API_LOG" | tr -d ' ')" == 4 ]]
''')

    def test_invalid_dns_info_does_not_commit_or_post(self):
        self.run_scenario('''
DNS_INFO='{"upstream_dns": []}'
echo old-hash > "$DNS_CONFIG_MD5_FILE"
if reload_dns_on_dependency_change 10.0.0.1 '' ''; then exit 1; fi
[[ ! -e "$DNS_DEPENDENCY_STATE_FILE" ]]
[[ "$(cat "$DNS_CONFIG_MD5_FILE")" == old-hash ]]
[[ "$(wc -l < "$API_LOG" | tr -d ' ')" == 1 ]]
''')

    def run_scenario(self, scenario):
        with tempfile.TemporaryDirectory() as tmp:
            script = '''
set -e
source "$1"
DNS_DEPENDENCY_STATE_FILE="$TEST_DIR/state"
DNS_CONFIG_MD5_FILE="$TEST_DIR/md5"
API_LOG="$TEST_DIR/api-log"
FAIL_POST=0
DNS_INFO='{"upstream_dns": ["coredns.antizapret", "[/*.antizapret/]127.0.0.11"]}'
adguard_api() {
    echo "$1" >> "$API_LOG"
    if [[ "$1" == /control/dns_info ]]; then
        printf '%s' "$DNS_INFO"
    else
        [[ "$FAIL_POST" == 0 ]] || return 22
        [[ "$*" == *'"[/*.antizapret/]127.0.0.11"'* ]]
    fi
}
'''
            result = subprocess.run(
                ["bash", "-c", script + scenario, "test", str(ROOT / "dns-reload.sh")],
                env={**os.environ, "TEST_DIR": tmp}, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
