# Multi-Threaded HTTP Server

## 📌 Overview
This project implements a **multi-threaded HTTP/1.1 server** from scratch using Python and low-level TCP socket programming.  
The server supports:
- **GET** requests for serving static HTML files and binary file downloads (PNG, JPEG, TXT).
- **POST** requests with JSON payloads (saved to `/resources/uploads`).
- **Thread pool concurrency** with request queuing.
- **Persistent connections** with Keep-Alive and connection timeouts.
- **Security protections** against path traversal and host header attacks.

---

## 🚀 Build & Run Instructions

### Requirements
- Python 3.8+
- No external dependencies (only standard library)

### Directory Structure
```
project/
├── server.py
├── resources/
│   ├── index.html
│   ├── about.html
│   ├── contact.html
│   ├── sample.txt
│   ├── logo.png
│   ├── photo.jpg
│   └── uploads/   (auto-created for POST uploads)
```

### Running the Server
```bash
# Default run (localhost:8080, 10 worker threads)
python server.py

# Custom port, host, and thread pool size
python server.py <port> <host> <max_threads>

# Example:
python server.py 8000 0.0.0.0 20
```

### Testing
You can test with:
- Browser (`http://127.0.0.1:8080`)
- `curl`  
  ```bash
  curl http://127.0.0.1:8080/about.html
  curl -O http://127.0.0.1:8080/logo.png
  curl -X POST -H "Content-Type: application/json" \
       -d '{"name":"Test"}' http://127.0.0.1:8080/upload
  ```

---

## 📂 Binary Transfer Implementation
- Files are read in **binary mode** using 8KB chunks to ensure large files transfer without memory issues.
- For `.png`, `.jpg`, `.jpeg`, `.txt` → served as **`application/octet-stream`** with:
  ```
  Content-Disposition: attachment; filename="file.ext"
  ```
  → forces browser download.
- For `.html` → served as **`text/html; charset=utf-8`** and rendered in browser.
- Unsupported file types return `415 Unsupported Media Type`.

---

## 🧵 Thread Pool Architecture
- **Main thread**:
  - Accepts incoming TCP connections.
  - Pushes `(socket, address)` into a **connection queue**.
- **Thread pool**:
  - Fixed-size pool (default 10 threads).
  - Worker threads consume from queue and process requests.
- **Connection queue**:
  - If all workers are busy and queue fills → return `503 Service Unavailable`.
- **Thread safety**:
  - Connection counter and logging are protected with locks.
  - Each client handled fully by one worker to avoid race conditions.

---

## 🔐 Security Measures
1. **Path Traversal Protection**
   - Requests containing `..`, `./`, or absolute paths are rejected.
   - Canonical paths are validated to remain inside `/resources`.
   - Blocked requests → `403 Forbidden`.

2. **Host Header Validation**
   - Only accepts `Host` headers matching:
     - `localhost:<port>`  
     - `127.0.0.1:<port>`
   - Missing Host → `400 Bad Request`.
   - Invalid Host → `403 Forbidden`.

3. **Connection Limits**
   - Persistent connections idle >30s are closed.
   - Max 100 requests per connection enforced.

4. **Logging**
   - All requests, security violations, and errors logged with timestamps.

---

## ⚠️ Known Limitations
- No HTTPS/TLS support (plain HTTP only)
- Only basic request parsing (`Content-Length` required, no chunked encoding).
- Only supports `.html`, `.txt`, `.png`, `.jpg/.jpeg`.
- Graceful shutdown is limited (workers are daemon threads).
