#!/usr/bin/env python3
"""Read Pi joystick events and display them. No motor or camera imports."""
import array
import fcntl
import json
import os
import select
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

EVENT=struct.Struct('IhBB')
state={'connected':False,'axes':{},'buttons':{},'live_events':0,'last_event':None,'recent_buttons':{},'history':[]}
lock=threading.Lock()
PAGE=r'''<!doctype html><meta charset="utf-8"><title>게임패드 입력 확인</title>
<style>body{font:20px system-ui;background:#10202b;color:#eef;max-width:850px;margin:40px auto;padding:20px}section{margin:20px 0}.button{display:inline-block;border:2px solid #8093a0;border-radius:10px;padding:16px;margin:5px}.pressed{background:#2ba776}progress{width:250px}pre{white-space:pre-wrap}</style>
<h1>Pi 게임패드 입력 확인</h1><p>모터는 움직이지 않습니다. 왼쪽·오른쪽 스틱을 움직이고 버튼을 하나씩 눌러보세요.</p><h2 id="name">연결 중</h2><section id="axes"></section><section id="buttons"></section><pre id="info"></pre>
<script>let busy=false;setInterval(async()=>{if(busy)return;busy=true;try{let s=await(await fetch('/status')).json();document.getElementById('name').textContent=s.connected?s.name:'연결 끊김';document.getElementById('axes').innerHTML=Object.entries(s.axes).map(([k,v])=>'<div>축 '+k+' <progress max="65534" value="'+(v+32767)+'"></progress> '+v+'</div>').join('');document.getElementById('buttons').innerHTML=Object.entries(s.buttons).map(([k,v])=>'<span class="button '+((v||Date.now()/1000-(s.recent_buttons[k]||0)<1)?'pressed':'')+'">'+k+'</span>').join('');document.getElementById('info').textContent='실제 입력 이벤트: '+s.live_events+'\n마지막 입력: '+JSON.stringify(s.last_event)+'\n최근 입력:\n'+s.history.slice(-8).map(e=>e.type+' '+e.number+' = '+e.value).join('\n')+'\n'+(s.error||'')}catch(e){document.getElementById('name').textContent='서버 연결 끊김'}finally{busy=false}},100);</script>'''

def monitor():
    fd=None
    try:
        fd=os.open('/dev/input/js0',os.O_RDONLY|os.O_NONBLOCK)
        name=bytearray(128);fcntl.ioctl(fd,0x80806a13,name)
        axes=array.array('B',[0]);buttons=array.array('B',[0])
        fcntl.ioctl(fd,0x80016a11,axes,True);fcntl.ioctl(fd,0x80016a12,buttons,True)
        with lock:state.update(connected=True,name=name.split(b'\0')[0].decode(),axis_count=axes[0],button_count=buttons[0])
        while True:
            if not select.select([fd],[],[],1)[0]:continue
            data=os.read(fd,EVENT.size)
            if len(data)!=EVENT.size:raise RuntimeError('게임패드 연결이 끊겼습니다')
            _,value,kind,number=EVENT.unpack(data)
            initial=bool(kind&0x80);kind&=0x7f
            with lock:
                if kind==2:state['axes'][number]=value
                if kind==1:state['buttons'][number]=value
                if not initial:
                    state['live_events']+=1
                    state['last_event']={'type':'axis' if kind==2 else 'button','number':number,'value':value,'time':time.time()}
                    state['history']=(state['history']+[state['last_event']])[-20:]
                    if kind==1 and value:state['recent_buttons'][number]=time.time()
    except Exception as exc:
        with lock:state.update(connected=False,error=str(exc))
    finally:
        if fd is not None:os.close(fd)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/':body=PAGE.encode();kind='text/html; charset=utf-8'
        elif self.path=='/status':
            with lock:body=json.dumps(state,ensure_ascii=False).encode()
            kind='application/json'
        else:self.send_error(404);return
        self.send_response(200);self.send_header('Content-Type',kind)
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass

if __name__=='__main__':
    threading.Thread(target=monitor,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',8082),Handler).serve_forever()
