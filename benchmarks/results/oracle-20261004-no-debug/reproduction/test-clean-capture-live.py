"""Live kernel filter acceptance with synthetic local UDP; no transfer benchmark."""
import json
import argparse
import socket
import subprocess
import time
from pathlib import Path

base=Path('/home/wasilij/rustytransfer-bench')
p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,default=base/'results/clean-observer-live-test-20261004.json')
output=p.parse_args().output
assert not output.exists()
helper=Path(__file__).with_name('clean-endpoint-observer.py')
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as receiver:
    receiver.bind(('127.0.0.1',0))
    port=receiver.getsockname()[1]
    process=subprocess.Popen(['/mnt/c/Windows/System32/wsl.exe','-d','Ubuntu','-u','root','--',
                              'python3',str(helper),'capture-udp','--peer','127.0.0.1',
                              '--port',str(port),'--output',str(output),'--count','8','--timeout','10'],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    time.sleep(1)
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
        for _ in range(8):
            sender.sendto(b'\0'*20,('127.0.0.1',port))
            receiver.recvfrom(1400)
        for _ in range(8):
            sender.sendto(b'\0'*1200,('127.0.0.1',port))
            receiver.recvfrom(1400)
    stdout,stderr=process.communicate(timeout=15)
    assert process.returncode==0,stderr.decode(errors='replace')
record=json.loads(output.read_text())
assert record['complete'] and record['observed_packets']==8
assert all(row['source_ip']=='127.0.0.1' and row['destination_port']==port and row['udp_bytes']==1208 for row in record['packets'])
print('PASS: live kernel BPF filter captured only expected header-sized UDP samples through root WSL interop.')
