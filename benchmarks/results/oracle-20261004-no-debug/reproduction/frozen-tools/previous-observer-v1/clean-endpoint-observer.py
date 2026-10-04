"""External launch/socket observations for normal CLI transfers, with no app debug controls."""
import argparse
import ctypes
import ipaddress
import json
import os
import socket
import struct
import time
from pathlib import Path

TIME_FORMAT = '{"user_cpu_seconds":%U,"system_cpu_seconds":%S,"max_rss_kib":%M,"process_seconds":%e}'
DEBUG_ENV = {'RUSTYTRANSFER_METRICS_JSONL', 'RUST_LOG', 'RUST_BACKTRACE', 'GODEBUG', 'GOTRACEBACK', 'CROC_DEBUG'}
PROXY_ENV = {'CROC_RELAY', 'CROC_RELAY6', 'SOCKS5_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
             'http_proxy', 'https_proxy', 'all_proxy'}


def debug_env(name):
    return name in DEBUG_ENV or name.startswith(('RUSTYTRANSFER_BENCH_', 'CROC_BENCH_'))


def launch(args):
    argv = args.argv
    if argv and argv[0] == '--':
        argv = argv[1:]
    assert not any(value in ('--debug', '--verbose', '--trace', '-v') for value in argv)
    environment = {k:v for k,v in os.environ.items() if not debug_env(k) and k not in PROXY_ENV}
    Path(args.pid_path).write_text(str(os.getpid())+'\n')
    os.execve('/usr/bin/time', ['/usr/bin/time', '-f', TIME_FORMAT, '-o', str(args.time_path),
                             str(args.exe), *argv], environment)


def address(text, ipv6):
    host, port = text.split(':')
    if ipv6:
        packed = b''.join(int(host[i:i+8],16).to_bytes(4,'little') for i in range(0,32,8))
        ip = ipaddress.IPv6Address(packed)
        host = str(ip.ipv4_mapped or ip)
    else:
        host = socket.inet_ntop(socket.AF_INET, int(host,16).to_bytes(4,'little'))
    return host, int(port,16)


def sockets(inodes):
    result = {'tcp':[], 'udp':[]}
    for protocol in ('tcp','tcp6','udp','udp6'):
        path = Path('/proc/net') / protocol
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            inode = fields[9]
            if inode not in inodes:
                continue
            local_ip,local_port = address(fields[1], protocol.endswith('6'))
            remote_ip,remote_port = address(fields[2], protocol.endswith('6'))
            state = {'01':'ESTABLISHED','0A':'LISTEN','07':'UNCONNECTED'}.get(fields[3],fields[3])
            result[protocol[:3]].append(dict(local_ip=local_ip,local_port=local_port,
                                             remote_ip=remote_ip,remote_port=remote_port,
                                             state=state,inode=inode))
    return result


def snapshot(args):
    expected = Path(args.exe).resolve()
    candidates=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            pid=int(proc.name)
            if os.getpgid(pid)!=args.pgid or Path(os.readlink(proc/'exe')).resolve()!=expected:
                continue
            raw_argv=[item.decode() for item in (proc/'cmdline').read_bytes().split(b'\0') if item]
            argv=list(raw_argv)
            for index,value in enumerate(argv):
                if value.startswith('rt1:') or (index and argv[index-1] in ('--invite','--password','--code')):
                    argv[index]='<redacted>'
            environment=[item.split(b'=',1)[0].decode() for item in (proc/'environ').read_bytes().split(b'\0') if item]
            inodes=set()
            for fd in (proc/'fd').iterdir():
                try:
                    target=os.readlink(fd)
                except OSError:
                    continue
                if target.startswith('socket:['):
                    inodes.add(target[8:-1])
            candidates.append(dict(pid=pid,pgid=args.pgid,exe=str(expected),cwd=os.readlink(proc/'cwd'),
                                   argv_redacted=argv,
                                   debug_flags_present=[value for value in raw_argv if value in ('--debug','--verbose','--trace','-v')],
                                   debug_env_present=sorted(name for name in environment if debug_env(name)),
                                   **sockets(inodes)))
        except (OSError,PermissionError,ProcessLookupError):
            continue
    assert len(candidates)<=1, 'Ambiguous benchmark endpoint identity'
    print(json.dumps(dict(observed_at_monotonic=time.monotonic(),process=candidates[0] if candidates else None)))


class Filter(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_=[('length',ctypes.c_ushort),('filters',ctypes.POINTER(Filter))]


def udp_filter(peer,port):
    # Ethernet IPv4, UDP, either direction of the expected peer/local-port pair,
    # IPv4 total length >=1000. Kernel filtering returns only a 96-byte header.
    peer_integer=int(ipaddress.IPv4Address(peer))
    instructions=[
        (0x28,0,0,12),           # EtherType
        (0x15,0,17,0x0800),      # IPv4 else reject at19
        (0x30,0,0,23),           # IP protocol
        (0x15,0,15,17),          # UDP else reject
        (0x28,0,0,16),           # IP total length
        (0x35,0,13,1000),        # >=1000 else reject
        (0xb1,0,0,14),           # X = IP header length
        (0x20,0,0,26),           # IP source
        (0x15,0,3,peer_integer),  # source==peer -> check local destination
        (0x48,0,0,16),           # UDP destination = Ethernet14 + X + 2
        (0x15,7,8,port),         # matches -> accept at18; else reject19
        (0x06,0,0,0),            # unreachable reject
        (0x20,0,0,30),           # IP destination
        (0x15,0,5,peer_integer),  # destination==peer else reject19
        (0x48,0,0,14),           # UDP source
        (0x15,2,3,port),         # matches -> accept18; else reject19
        (0x06,0,0,0),
        (0x06,0,0,0),
        (0x06,0,0,96),           # accept header only
        (0x06,0,0,0),
    ]
    return (Filter*len(instructions))(*(Filter(*entry) for entry in instructions))


def packet_header(data):
    if len(data)<42 or data[12:14]!=b'\x08\x00' or data[23]!=17:
        return None
    header_length=(data[14]&15)*4
    if header_length<20 or len(data)<14+header_length+8:
        return None
    source_ip=socket.inet_ntop(socket.AF_INET,data[26:30])
    destination_ip=socket.inet_ntop(socket.AF_INET,data[30:34])
    source_port,destination_port,udp_length=struct.unpack('!HHH',data[14+header_length:20+header_length])
    return dict(source_ip=source_ip,destination_ip=destination_ip,source_port=source_port,
                destination_port=destination_port,ip_bytes=struct.unpack('!H',data[16:18])[0],udp_bytes=udp_length)


def capture_udp(args):
    peer=str(ipaddress.IPv4Address(args.peer))
    assert 1<=args.port<=65535 and 1<=args.count<=128 and 0<args.timeout<=30
    output=Path(args.output).resolve()
    assert output.is_relative_to(Path('/home/wasilij/rustytransfer-bench')) or output.is_relative_to(Path('/home/ubuntu/rustytransfer-bench'))
    assert not output.exists() and output.parent.is_dir()
    started=time.monotonic()
    packets=[]
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3)) as sock:
        filters=udp_filter(peer,args.port)
        program=Program(len(filters),filters)
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.setsockopt(sock.fileno(),socket.SOL_SOCKET,26,ctypes.byref(program),ctypes.sizeof(program))!=0:
            raise OSError(ctypes.get_errno(),'SO_ATTACH_FILTER')
        sock.settimeout(min(args.timeout,.5))
        while len(packets)<args.count and time.monotonic()-started<args.timeout:
            try:
                data,origin=sock.recvfrom(96)
            except socket.timeout:
                continue
            header=packet_header(data)
            if header is None:
                continue
            valid=(header['source_ip']==peer and header['destination_port']==args.port) or (header['destination_ip']==peer and header['source_port']==args.port)
            assert valid and header['ip_bytes']>=1000, header
            packets.append(dict(elapsed_seconds=time.monotonic()-started,interface=origin[0],packet_type=origin[2],**header))
    record=dict(kind='external-sampled-direct-ipv4-udp',peer=peer,local_port=args.port,
                requested_packets=args.count,observed_packets=len(packets),complete=len(packets)==args.count,
                observer_seconds=time.monotonic()-started,packets=packets,
                limitation='Header samples identify actual peer UDP bulk traffic at observation times; they do not decrypt QUIC STREAM frames or establish continuous route coverage.')
    output.write_text(json.dumps(record,indent=2)+'\n')
    os.chmod(output,0o644)
    print(json.dumps({key:value for key,value in record.items() if key!='packets'}))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    commands=p.add_subparsers(dest='mode',required=True)
    launch_parser=commands.add_parser('launch')
    launch_parser.add_argument('--pid-path',required=True,type=Path)
    launch_parser.add_argument('--time-path',required=True,type=Path)
    launch_parser.add_argument('--exe',required=True,type=Path)
    launch_parser.add_argument('argv',nargs=argparse.REMAINDER)
    observe=commands.add_parser('snapshot')
    observe.add_argument('--pgid',required=True,type=int)
    observe.add_argument('--exe',required=True,type=Path)
    capture=commands.add_parser('capture-udp')
    capture.add_argument('--peer',required=True)
    capture.add_argument('--port',required=True,type=int)
    capture.add_argument('--output',required=True,type=Path)
    capture.add_argument('--count',type=int,default=32)
    capture.add_argument('--timeout',type=float,default=10)
    args=p.parse_args()
    {'launch':launch,'snapshot':snapshot,'capture-udp':capture_udp}[args.mode](args)


if __name__=='__main__':
    main()
