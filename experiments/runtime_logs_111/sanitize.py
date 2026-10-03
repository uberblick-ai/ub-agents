"""Re-sanitize our existing private capture; never starts a runtime."""
import hashlib
import json

from .capture import PRIVATE, EVIDENCE, scrubber


def main():
    for runtime in ('claude', 'codex'):
        folder = EVIDENCE / runtime
        raw = (PRIVATE / runtime / 'run/process.log').read_bytes()
        arrivals = json.loads((folder / 'arrivals.json').read_text())
        scrub = scrubber(PRIVATE / runtime / 'fixture')
        chunks = []
        end = 0
        lines = raw.splitlines(keepends=True)
        assert len(lines) == len(arrivals)
        for line, arrival in zip(lines, arrivals):
            try:
                content = json.dumps(scrub(json.loads(line)), ensure_ascii=False)
            except (ValueError, UnicodeError):
                content = scrub(line.rstrip(b'\n').decode('utf-8', errors='replace'))
            chunk = content.encode() + (b'\n' if line.endswith(b'\n') else b'')
            chunks.append(chunk)
            end += len(chunk)
            arrival['end'] = end
        public = b''.join(chunks)
        (folder / 'process.log').write_bytes(public)
        (folder / 'arrivals.json').write_text(json.dumps(arrivals, indent=2) + '\n')
        meta = json.loads((folder / 'capture.json').read_text())
        meta['sanitized_bytes'] = len(public)
        meta['sanitized_sha256'] = hashlib.sha256(public).hexdigest()
        meta['sanitization'] = 'entire stream; no records selected or added; paths, IDs, credential-like values, email, socket, opaque signatures and account quota metadata replaced; JSON reserialized'
        (folder / 'capture.json').write_text(json.dumps(meta, indent=2) + '\n')


if __name__ == '__main__':
    main()
