"""Opt-in boundary probe; it is not an evaluation runner or a security certification."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.environ.get("DEVFLOW_TEST_EVALUATION_CONTAINER") != "1",
                    reason="Requires an explicitly enabled local Docker probe")
def test_candidate_container_has_no_network_or_private_mount_and_is_read_only(tmp_path: Path):
    candidate = tmp_path / "candidate.pl"
    candidate.write_text('''use strict;
use warnings;
use JSON::PP;
use IO::Socket::INET;
my %result = (uid => 0+$<, private_visible => -e "/private" ? JSON::PP::true : JSON::PP::false,
    docker_socket_visible => -e "/var/run/docker.sock" ? JSON::PP::true : JSON::PP::false);
for my $entry ([candidate_write => "/candidate.pl"], [root_write => "/outside"]) {
    my $ok = open(my $fh, ">", $entry->[1]);
    close($fh) if $ok;
    $result{$entry->[0]} = $ok ? JSON::PP::true : JSON::PP::false;
}
my $socket = IO::Socket::INET->new(PeerAddr => "1.1.1.1", PeerPort => 443, Timeout => 2);
$result{network} = $socket ? JSON::PP::true : JSON::PP::false;
print encode_json(\\%result);
''', encoding="utf-8")
    before = candidate.read_bytes()
    result = subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--memory", "128m", "--cpus", "1",
        "--user", "65534:65534", "--mount",
        f"type=bind,source={candidate},destination=/candidate.pl,readonly",
        "--entrypoint", "perl", "postgres:17.11", "/candidate.pl",
    ], capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(result.stdout) == {
        "uid": 65534, "private_visible": False, "docker_socket_visible": False,
        "candidate_write": False, "root_write": False, "network": False,
    }
    assert candidate.read_bytes() == before
