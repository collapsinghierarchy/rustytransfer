"""Meaningful acceptance tests for external packet filtering and environment cleaning."""
import importlib.util
import json
import os
import socket
import struct
import subprocess
import tempfile
from pathlib import Path

spec=importlib.util.spec_from_file_location('observer',Path(__file__).with_name('clean-endpoint-observer.py'))
observer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


def frame(source='127.0.0.1',destination='10.0.0.2',sport=22222,dport=33333,protocol=17,total=1400):
    ip=struct.pack('!BBHHHBBH4s4s',0x45,0,total,0,0,64,protocol,0,socket.inet_aton(source),socket.inet_aton(destination))
    udp=struct.pack('!HHHH',sport,dport,total-20,0)
    return b'\0'*12+b'\x08\x00'+ip+udp+b'\0'*max(0,total-28)


def interpret(filters,data):
    accumulator=0
    index=0
    offset=0
    while index<len(filters):
        instruction=filters[index]
        code=instruction.code
        if code in (0x28,0x30,0x20,0x48):
            start=instruction.k+(offset if code==0x48 else 0)
            length={0x28:2,0x30:1,0x20:4,0x48:2}[code]
            if start+length>len(data):
                return 0
            accumulator=int.from_bytes(data[start:start+length],'big')
        elif code==0xb1:
            offset=(data[instruction.k]&15)*4
        elif code in (0x15,0x35):
            match=accumulator==instruction.k if code==0x15 else accumulator>=instruction.k
            index+=instruction.jt if match else instruction.jf
        elif code==0x06:
            return instruction.k
        else:
            raise AssertionError(code)
        index+=1
    raise AssertionError('BPF ran past return')


filters=observer.udp_filter('127.0.0.1',33333)
assert interpret(filters,frame())==96
assert interpret(filters,frame(source='10.0.0.2',destination='127.0.0.1',sport=33333,dport=22222))==96
for packet in (frame(source='127.0.0.2'),frame(dport=33334),frame(protocol=6),frame(total=999),b'\0'*8):
    assert interpret(filters,packet)==0
header=observer.packet_header(frame())
assert header['source_ip']=='127.0.0.1' and header['destination_port']==33333 and header['udp_bytes']==1380
assert observer.address('0100007F:232F',False)==('127.0.0.1',9007)
with tempfile.TemporaryDirectory(dir='/home/wasilij/rustytransfer-bench') as directory:
    root=Path(directory)
    script=root/'env.py'
    script.write_text("import os,json; print(json.dumps({'forbidden':[k for k in os.environ if k.startswith(('RUSTYTRANSFER_BENCH_','CROC_BENCH_')) or k in ('RUST_LOG','RUSTYTRANSFER_METRICS_JSONL','GODEBUG')],'normal_credential_present':'CROC_SECRET' in os.environ}))")
    environment=os.environ|{'RUSTYTRANSFER_BENCH_WAIT_DIRECT':'1','RUSTYTRANSFER_METRICS_JSONL':'ignored','RUST_LOG':'trace','GODEBUG':'ignored','CROC_BENCH_PHASE_PROFILE':'1','CROC_SECRET':'test-fixture-secret'}
    command=['python3',str(Path(observer.__file__)),'launch','--pid-path',str(root/'pid'),
             '--time-path',str(root/'time.json'),'--exe','/usr/bin/python3','--',str(script)]
    result=subprocess.run(command,env=environment,capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'forbidden':[],'normal_credential_present':True}
    assert json.loads((root/'time.json').read_text())['process_seconds']>=0
print('PASS: peer/port/UDP/length filter acceptance and rejection; NAT address decoding; inherited debug/profile environment stripped; ordinary credential preserved; GNU time captured.')
