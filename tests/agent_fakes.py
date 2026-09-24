"""本地 OpenAI 服务；测试运行真实 Python agent SDK 和 HTTP 工具协议。"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class AgentServer:
    def __init__(self):
        self.callback = None
        self.payloads = []
        self.errors = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    assert self.path == '/v1/chat/completions'
                    assert self.headers['Authorization'] == 'Bearer offline-key'
                    assert payload.get('stream') is not True and 'tools' in payload
                    owner.payloads.append(payload)
                    images = [part['image_url']['url'] for message in payload['messages']
                              if isinstance(message.get('content'), list) for part in message['content']
                              if part.get('type') == 'image_url']
                    assert len(images) == 1
                    png = base64.b64decode(images[0].split(',')[1])
                    answer = owner.callback(png, json.dumps(payload, ensure_ascii=False))
                    if isinstance(answer, dict) and 'http_status' in answer:
                        self.send_error(answer['http_status'], 'temporary model failure')
                        return
                    # 简写仅用于通用目标测试；业务证据测试直接给出原生工具参数。
                    status = ('reached' if answer.get('reached') is True else 'pending'
                              if answer.get('reached') is False else answer.get('reached')) if isinstance(answer, dict) else None
                    evidence = {'status': status, 'observation': answer.get('observation')} if isinstance(answer, dict) else {}
                    if isinstance(answer, dict) and answer.get('reached') is False and answer.get('action') is not None:
                        name, args = 'move', {**evidence, 'direction': answer.get('action')}
                    elif isinstance(answer, dict) and 'reached' in answer and answer.get('action') is None:
                        name, args = 'finish', evidence
                    else:
                        name, args = 'invalid', {}
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [
                        tool_call(name, args, f'call-{len(owner.payloads)}')]}
                    if isinstance(answer, dict) and 'message' in answer:
                        message = answer['message']
                    chunk = {'id': 'offline', 'object': 'chat.completion', 'created': 1, 'model': payload['model'],
                             'choices': [{'index': 0, 'message': message,
                                 'finish_reason': answer.get('finish_reason', 'tool_calls')
                                  if isinstance(answer, dict) else 'tool_calls'}]}
                    body = json.dumps(chunk, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    owner.errors.append(exc)
                    self.send_error(500, 'offline failure')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()

    def connection(self, callback):
        self.callback = callback
        return dict(base_url=f'http://127.0.0.1:{self.server.server_port}/v1', model='offline',
                    api_key='offline-key', timeout_seconds=2)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def tool_call(name, args, call_id='offline-call'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
