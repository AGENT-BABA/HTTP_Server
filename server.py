import sys
import socket
import threading
import queue
import os
import errno
import datetime
import json
import random
import string
import logging
from urllib.parse import unquote

# --------------------------- Configuration ---------------------------

# Default config values 

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8080
DEFAULT_MAX_THREADS = 10
LISTEN_BACKLOG = 50
MAX_REQUEST_SIZE = 8192  # Not let clients spam us with huge requests
RESOURCES_DIR = os.path.join(os.path.dirname(__file__), 'resources')
UPLOADS_DIR = os.path.join(RESOURCES_DIR, 'uploads')
CONN_TIMEOUT = 30        # keep-alive connection timeout
CONN_MAX_REQS = 100      # Max requests allowed per persistent connection
QUEUE_MAXSIZE = 200      # Max queued connections before we start rejecting
SERVER_NAME = 'Multi-threaded HTTP Server'
BUFFER_SIZE = 8192       # Used when streaming files in binary mode

# --------------------------- Logging Setup ---------------------------

#Logs with timestamps

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger('mt-http-server')

# --------------------------- Utility Helpers ---------------------------

def current_rfc7231_date():
    """Return current date format in RFC 7231 (used in HTTP headers)."""

    now = datetime.datetime.utcnow()
    return now.strftime('%a, %d %b %Y %H:%M:%S GMT')


def random_id(n=4):

    # Random string generator for naming uploads
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


# --------------------------- HTTP Helpers ---------------------------

class HTTPRequest:
    # Container for holding HTTP request data
    def __init__(self, method='', path='/', version='HTTP/1.1'):
        self.method = method
        self.path = path
        self.version = version
        self.headers = {}
        self.body = b''


def parse_http_request(raw_bytes):
    """
    Parse an HTTP request (raw bytes) into an HTTPRequest object.
    Throws ValueError if anything looks bad.
    """
    try:
        # Decode raw bytes -> text (ISO-8859-1 keeps all header bytes intact)
        text = raw_bytes.decode('iso-8859-1')
    except Exception:
        raise ValueError('Unable to decode request')

    # Split headers vs body using the blank line separator

    parts = text.split('\r\n\r\n', 1)
    header_block = parts[0]
    body_part = parts[1] if len(parts) > 1 else ''

    lines = header_block.split('\r\n')
    if len(lines) == 0:
        raise ValueError('Empty request')

    # First line should be: METHOD /path HTTP/1.1 as mentioned in DOC 

    request_line = lines[0].split()
    if len(request_line) != 3:
        raise ValueError('Malformed request line')

    method, path, version = request_line
    req = HTTPRequest(method=method, path=path, version=version)

    # Parsing headers 
    
    for header in lines[1:]:
        if ':' not in header:
            continue
        name, value = header.split(':', 1)
        req.headers[name.strip().lower()] = value.strip()

    # Body (raw text -> bytes again for safe handling)
    
    req.body = body_part.encode('iso-8859-1')
    return req


def build_response(status_code, reason, headers=None, body=b''):
    """
    Build a proper HTTP response with status line, headers, and body.
    """
    
    if headers is None:
        headers = {}
    status_line = f'HTTP/1.1 {status_code} {reason}\r\n'
    header_lines = ''
    for k, v in headers.items():
        header_lines += f'{k}: {v}\r\n'
    response_head = (status_line + header_lines + '\r\n').encode('iso-8859-1')
    if isinstance(body, str):
        body = body.encode('utf-8')
    return response_head + body


# --------------------------- Security Measures ---------------------------

def is_safe_path(base_dir, user_path):
    """
    Make sure the requested path stays inside our base_dir.
    This stops something like `../../../etc/passwd`.
    """
    if '..' in user_path or user_path.startswith('/') or user_path.startswith('\\') or user_path.startswith('\\\\'):
        return False, None

    # Strip query strings and fragments
    
    user_path = user_path.split('?', 1)[0].split('#', 1)[0]
    user_path = unquote(user_path)      # Decode %20 etc.
    user_path = user_path.lstrip('/')   # Drop leading slashes

    final_path = os.path.realpath(os.path.join(base_dir, user_path))
    base_real = os.path.realpath(base_dir)
    if not final_path.startswith(base_real):
        return False, None
    return True, final_path


# --------------------------- File Handling ---------------------------

def guess_mime_and_mode(filename):
    """
    Guess MIME type based on file extension.
    mode = 'text' means send in one shot, 'binary' means chunked streaming.
    """
    
    lower = filename.lower()
    if lower.endswith('.html') or lower.endswith('.htm'):
        return 'text/html; charset=utf-8', 'text'
    if lower.endswith('.txt'):
        return 'application/octet-stream', 'binary'
    if lower.endswith('.png') or lower.endswith('.jpg') or lower.endswith('.jpeg'):
        return 'application/octet-stream', 'binary'
    return None, None


# --------------------------- Thread Pool & Worker ---------------------------

class ThreadPoolServer:
    """
    - Main thread: accepts new connections, puts them in a queue
    - Worker threads: pick up connections and handle them
    """
    def __init__(self, host, port, max_threads=DEFAULT_MAX_THREADS):
        self.host = host
        self.port = port
        self.max_threads = max_threads
        self.socket = None
        self.running = False
        self.conn_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.workers = []
        self.active_connections_lock = threading.Lock()
        self.active_connections = 0

    def start(self):
        # Ensururing folder existexnce
       
        os.makedirs(RESOURCES_DIR, exist_ok=True)
        os.makedirs(UPLOADS_DIR, exist_ok=True)

        # Create listening socket

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        self.socket.listen(LISTEN_BACKLOG)
        self.running = True

        logger.info(f'HTTP Server started on http://{self.host}:{self.port}')
        logger.info(f'Thread pool size: {self.max_threads}')
        logger.info(f"Serving files from '{RESOURCES_DIR}'")
        logger.info('Press Ctrl+C to stop the server')

        #Worker threads
        
        for i in range(self.max_threads):
            t = threading.Thread(target=self.worker_loop, name=f'Thread-{i+1}', daemon=True)
            t.start()
            self.workers.append(t)

        # Main accept loop
        try:
            while self.running:
                try:
                    client_sock, addr = self.socket.accept()
                    logger.info(f'[Main] Incoming connection from {addr[0]}:{addr[1]}')
                    try:
                        self.conn_queue.put_nowait((client_sock, addr))
                        logger.info(f'[Main] Connection queued (queue size: {self.conn_queue.qsize()})')
                    except queue.Full:
                        # Oops – too many waiting clients, reject politely
                        logger.warning('[Main] Thread pool saturated, rejecting connection with 503')
                        try:
                            resp = build_response(503, 'Service Unavailable', headers={
                                'Date': current_rfc7231_date(),
                                'Server': SERVER_NAME,
                                'Content-Length': '0',
                                'Connection': 'close',
                                'Retry-After': '5'
                            })
                            client_sock.sendall(resp)
                        except Exception:
                            pass
                        finally:
                            client_sock.close()
                except socket.error as e:
                    if e.errno == errno.EINTR:
                        continue
                    logger.error(f'[Main] Socket error: {e}')
        except KeyboardInterrupt:
            logger.info('Shutdown requested by user')
            self.running = False
        finally:
            self.shutdown()

    def shutdown(self):
        # Shutting down server 
        self.running = False
        try:
            if self.socket:
                self.socket.close()
        except Exception:
            pass
        logger.info('Server stopped')
    
    def worker_loop(self):
        """
        Worker threads live here: keep picking up connections from the queue
        and process them until the server shuts down.
        """

        thread_name = threading.current_thread().name
        while self.running:
            try:
                client_sock, addr = self.conn_queue.get(timeout=1)
            except queue.Empty:
                continue
            with self.active_connections_lock:
                self.active_connections += 1
            try:
                logger.info(f'[{thread_name}] Connection from {addr[0]}:{addr[1]}')
                self.handle_client(client_sock, addr, thread_name)
            except Exception as e:
                logger.exception(f'[{thread_name}] Error handling client: {e}')
            finally:
                with self.active_connections_lock:
                    self.active_connections -= 1
                try:
                    client_sock.close()
                except Exception:
                    pass
                self.conn_queue.task_done()


    def handle_client(self, client_sock, addr, thread_name):
        client_sock.settimeout(CONN_TIMEOUT)
        req_count = 0
        keep_alive = True
        peer = f'{addr[0]}:{addr[1]}'

        while keep_alive and req_count < CONN_MAX_REQS:
            try:
                
                # Read up to MAX_REQUEST_SIZE or until blank line

                data = b''
                client_sock.settimeout(CONN_TIMEOUT)
                while True:
                    chunk = client_sock.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                    if b'\r\n\r\n' in data or len(data) >= MAX_REQUEST_SIZE:
                        break
                if not data:
                    break

                if len(data) > MAX_REQUEST_SIZE:
                    logger.info(f'[{thread_name}] Request size > {MAX_REQUEST_SIZE} bytes, rejecting')
                    resp = build_response(400, 'Bad Request', headers={
                        'Date': current_rfc7231_date(),
                        'Server': SERVER_NAME,
                        'Content-Length': '0',
                        'Connection': 'close'
                    })
                    client_sock.sendall(resp)
                    break

                try:
                    req = parse_http_request(data)
                except ValueError:
                    logger.info(f'[{thread_name}] Malformed request')
                    resp = build_response(400, 'Bad Request', headers={
                        'Date': current_rfc7231_date(),
                        'Server': SERVER_NAME,
                        'Content-Length': '0',
                        'Connection': 'close'
                    })
                    client_sock.sendall(resp)
                    break

                logger.info(f'[{thread_name}] Request: {req.method} {req.path} {req.version}')

                # Host header validation
                host_header = req.headers.get('host')
                if host_header is None:
                    logger.info(f'[{thread_name}] Missing Host header')
                    resp = build_response(400, 'Bad Request', headers={
                        'Date': current_rfc7231_date(),
                        'Server': SERVER_NAME,
                        'Content-Length': '0',
                        'Connection': 'close'
                    })
                    client_sock.sendall(resp)
                    break
                expected_host1 = f'{self.host}:{self.port}'
                expected_host2 = f'{self.host}'
                if host_header not in (expected_host1, expected_host2, 'localhost:'+str(self.port), 'localhost') and host_header != '127.0.0.1:'+str(self.port):
                    logger.warning(f'[{thread_name}] Host validation failed: {host_header}')
                    resp = build_response(403, 'Forbidden', headers={
                        'Date': current_rfc7231_date(),
                        'Server': SERVER_NAME,
                        'Content-Length': '0',
                        'Connection': 'close'
                    })
                    client_sock.sendall(resp)
                    break
                else:
                    logger.info(f'[{thread_name}] Host validation: {host_header} ✓')

                # Determine connection header behavior

                conn_hdr = req.headers.get('connection')
                if req.version == 'HTTP/1.0':
                    keep_alive = (conn_hdr and conn_hdr.lower() == 'keep-alive')
                else:
                    # HTTP/1.1 default keep-alive unless 'close'

                    keep_alive = not (conn_hdr and conn_hdr.lower() == 'close')

                # Process methods

                if req.method == 'GET':
                    self.handle_get(req, client_sock, thread_name, keep_alive)
                elif req.method == 'POST':

                    # Need to read full body based on Content-Length
                    content_length = int(req.headers.get('content-length', '0'))

                    # If body was partially included after headers

                    existing_body = req.body
                    if len(existing_body) < content_length:
                        to_read = content_length - len(existing_body)
                        body = existing_body
                        while to_read > 0:
                            more = client_sock.recv(min(8192, to_read))
                            if not more:
                                break
                            body += more
                            to_read -= len(more)
                        req.body = body
                    else:
                        req.body = existing_body[:content_length]
                    self.handle_post(req, client_sock, thread_name, keep_alive)
                else:
                    # Method not allowed
                    resp = build_response(405, 'Method Not Allowed', headers={
                        'Date': current_rfc7231_date(),
                        'Server': SERVER_NAME,
                        'Content-Length': '0',
                        'Connection': ('keep-alive' if keep_alive else 'close')
                    })
                    client_sock.sendall(resp)

                req_count += 1
                if req_count >= CONN_MAX_REQS:
                    keep_alive = False

                # when keep_alive, continue loop; otherwise break and close
                if not keep_alive:
                    break

            except socket.timeout:
                logger.info(f'[{thread_name}] Connection idle timeout, closing')
                break
            except Exception as e:
                logger.exception(f'[{thread_name}] Unexpected error: {e}')
                try:
                    resp = build_response(500, 'Internal Server Error', headers={
                        'Date': current_rfc7231_date(),
                        'Server': SERVER_NAME,
                        'Content-Length': '0',
                        'Connection': 'close'
                    })
                    client_sock.sendall(resp)
                except Exception:
                    pass
                break

    # --------------------------- Request Handlers ---------------------------

    def handle_get(self, req, client_sock, thread_name, keep_alive):
        # Default root -> index.html
        path = req.path
        if path == '/':
            path = '/index.html'

        safe, final_path = is_safe_path(RESOURCES_DIR, path)
        if not safe or final_path is None:
            logger.warning(f'[{thread_name}] Path traversal or invalid path attempt: {req.path}')
            resp = build_response(403, 'Forbidden', headers={
                'Date': current_rfc7231_date(),
                'Server': SERVER_NAME,
                'Content-Length': '0',
                'Connection': ('keep-alive' if keep_alive else 'close')
            })
            client_sock.sendall(resp)
            return

        if not os.path.exists(final_path) or not os.path.isfile(final_path):
            resp = build_response(404, 'Not Found', headers={
                'Date': current_rfc7231_date(),
                'Server': SERVER_NAME,
                'Content-Length': '0',
                'Connection': ('keep-alive' if keep_alive else 'close')
            })
            client_sock.sendall(resp)
            logger.info(f'[{thread_name}] Response: 404 Not Found')
            return

        mime, mode = guess_mime_and_mode(final_path)
        if mime is None:
            resp = build_response(415, 'Unsupported Media Type', headers={
                'Date': current_rfc7231_date(),
                'Server': SERVER_NAME,
                'Content-Length': '0',
                'Connection': ('keep-alive' if keep_alive else 'close')
            })
            client_sock.sendall(resp)
            logger.info(f'[{thread_name}] Response: 415 Unsupported Media Type')
            return

        file_size = os.path.getsize(final_path)
        headers = {
            'Date': current_rfc7231_date(),
            'Server': SERVER_NAME,
            'Content-Type': mime,
            'Content-Length': str(file_size),
            'Connection': ('keep-alive' if keep_alive else 'close')
        }

        # For binary downloads, add Content-Disposition for attachment

        if mode == 'binary':
            filename = os.path.basename(final_path)
            headers['Content-Disposition'] = f'attachment; filename="{filename}"'

        if keep_alive:
            headers['Keep-Alive'] = f'timeout={CONN_TIMEOUT}, max={CONN_MAX_REQS}'

        # Send headers
        head = build_response(200, 'OK', headers=headers, body=b'')
        client_sock.sendall(head)
        logger.info(f'[{thread_name}] Sending {mode} file: {os.path.basename(final_path)} ({file_size} bytes)')

        # Send file in chunks for binary; for text/html we can send in one go

        if mode == 'text':
            with open(final_path, 'rb') as f:
                data = f.read()
                client_sock.sendall(data)
        else:
            with open(final_path, 'rb') as f:
                while True:
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    client_sock.sendall(chunk)

        logger.info(f'[{thread_name}] Response: 200 OK ({file_size} bytes transferred)')

    def handle_post(self, req, client_sock, thread_name, keep_alive):

        # Only accept application/json
        ctype = req.headers.get('content-type', '')

        if 'application/json' not in ctype:
            resp = build_response(415, 'Unsupported Media Type', headers={
                'Date': current_rfc7231_date(),
                'Server': SERVER_NAME,
                'Content-Length': '0',
                'Connection': ('keep-alive' if keep_alive else 'close')
            })
            client_sock.sendall(resp)
            logger.info(f'[{thread_name}] Response: 415 Unsupported Media Type')
            return

        # Parse JSON

        try:
            body_text = req.body.decode('utf-8')
            obj = json.loads(body_text)
        except Exception:
            resp = build_response(400, 'Bad Request', headers={
                'Date': current_rfc7231_date(),
                'Server': SERVER_NAME,
                'Content-Length': '0',
                'Connection': ('keep-alive' if keep_alive else 'close')
            })
            client_sock.sendall(resp)
            logger.info(f'[{thread_name}] Response: 400 Bad Request (invalid JSON)')
            return

        # Prepare file name

        ts = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        fname = f'upload_{ts}_{random_id(4)}.json'
        relpath = os.path.join('uploads', fname)
        fullpath = os.path.join(UPLOADS_DIR, fname)

        try:
            with open(fullpath, 'w', encoding='utf-8') as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.exception(f'[{thread_name}] Failed to write upload: {e}')
            resp = build_response(500, 'Internal Server Error', headers={
                'Date': current_rfc7231_date(),
                'Server': SERVER_NAME,
                'Content-Length': '0',
                'Connection': 'close'
            })
            client_sock.sendall(resp)
            return

        # Build success response body
        
        resp_body = json.dumps({
            'status': 'success',
            'message': 'File created successfully',
            'filepath': '/' + relpath.replace('\\\\', '/')
        })
        body_bytes = resp_body.encode('utf-8')
        headers = {
            'Date': current_rfc7231_date(),
            'Server': SERVER_NAME,
            'Content-Type': 'application/json',
            'Content-Length': str(len(body_bytes)),
            'Connection': ('keep-alive' if keep_alive else 'close')
        }
        if keep_alive:
            headers['Keep-Alive'] = f'timeout={CONN_TIMEOUT}, max={CONN_MAX_REQS}'

        resp = build_response(201, 'Created', headers=headers, body=body_bytes)
        client_sock.sendall(resp)
        logger.info(f'[{thread_name}] Response: 201 Created -> {relpath}')


# --------------------------- Main Entrypoint ---------------------------

def main():
    port = DEFAULT_PORT
    host = DEFAULT_HOST
    max_threads = DEFAULT_MAX_THREADS
    if len(sys.argv) >= 2:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print('Invalid port number, using default')
    if len(sys.argv) >= 3:
        host = sys.argv[2]
    if len(sys.argv) >= 4:
        try:
            max_threads = int(sys.argv[3])
        except ValueError:
            print('Invalid thread count, using default')

    server = ThreadPoolServer(host, port, max_threads=max_threads)
    server.start()


if __name__ == '__main__':
    main()
