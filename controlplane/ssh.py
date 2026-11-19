"""Pinned-host SSH executor. No shell commands run on the panel host."""
import base64
import hashlib
import io
import ipaddress
import time
from fastapi import HTTPException
from .resources import observe


def execute(web, target, connection, script):
    import paramiko
    host = observe(web, target)[connection['address']]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(400, 'AWS instance has no usable selected IP address')
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        raise HTTPException(400, 'Unsafe instance address')
    if connection['address'] == 'public' and not address.is_global:
        raise HTTPException(400, 'Public SSH requires a global instance address')

    class PinnedPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
            if fingerprint != connection['fingerprint']:
                raise paramiko.SSHException('SSH host fingerprint mismatch')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedPolicy())
    key = None
    if connection.get('private_key'):
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                key = cls.from_private_key(io.StringIO(connection['private_key']))
                break
            except (paramiko.SSHException, ValueError):
                pass
        if key is None:
            raise HTTPException(400, 'Unsupported private key or encrypted key without passphrase')
    try:
        client.connect(str(address), port=connection['port'], username=connection['username'],
                       pkey=key, password=connection.get('password') or None,
                       allow_agent=False, look_for_keys=False, timeout=10, auth_timeout=10, banner_timeout=10)
        # Sending the body through stdin avoids shell interpolation of script content.
        stdin, stdout, stderr = client.exec_command('sudo -n bash -s' if connection['sudo'] else 'bash -s', timeout=10)
        stdin.write(script['body'])
        stdin.flush()
        stdin.channel.shutdown_write()
        channel = stdout.channel
        deadline, output, error = time.monotonic()+script['timeout'], bytearray(), bytearray()
        while True:
            for _ in range(16):
                if not channel.recv_ready():
                    break
                chunk = channel.recv(4096)
                output.extend(chunk[:max(0, 32768-len(output))])
            for _ in range(16):
                if not channel.recv_stderr_ready():
                    break
                chunk = channel.recv_stderr(4096)
                error.extend(chunk[:max(0, 32768-len(error))])
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('Remote execution timed out; command may still be running')
            time.sleep(0.05)
        # Output may include secrets. Stored encrypted, returned only by the dedicated result endpoint.
        return {'exit_code': channel.recv_exit_status(), 'stdout': output.decode(errors='replace'),
                'stderr': error.decode(errors='replace'), 'output_limit': 32768}
    finally:
        client.close()
