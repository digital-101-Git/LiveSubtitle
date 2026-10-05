"""Small CPU subprocesses only; no models, GPU, credentials or sockets."""
import io
import os
import subprocess
import sys
import threading
import time

import pytest

from engine.runtime_logging import PipeLogWriter


class TrackedPipe(io.BytesIO):
    def __init__(self, value, read_limit=None):
        super().__init__(value)
        self.requests=[]
        self.total_read=0
        self.read_limit=read_limit

    def read(self, size=-1):
        self.requests.append(size)
        value=super().read(min(size,self.read_limit) if self.read_limit else size)
        self.total_read+=len(value)
        return value


def wait_until(predicate, timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), 'Owned child/log operation did not complete'


def child(code):
    return subprocess.Popen([sys.executable,'-B','-u','-c',code],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=3)
    if process.stdin:
        process.stdin.close()


def test_live_child_log_can_be_deleted_and_next_output_recreates_it(tmp_path):
    process=child("import sys;print('before-delete',flush=True);sys.stdin.readline();print('after-delete',flush=True)")
    lock=threading.RLock()
    writer=PipeLogWriter(process.stdout,tmp_path,lock,secrets=('known-test-secret',))
    path=tmp_path/'logs/llama.log'
    writer.start()
    try:
        wait_until(lambda:path.exists() and b'before-delete' in path.read_bytes())
        with lock:
            path.unlink()  # Windows deletion succeeds because writer closed it.
        assert not path.exists()
        process.stdin.write(b'continue\n')
        process.wait(timeout=3)
        assert writer.finish()
        assert path.read_bytes().replace(b'\r\n',b'\n')==b'after-delete\n'
        assert process.stdout.closed and not writer.thread.is_alive()
    finally:
        stop_child(process)
        writer.finish()


def test_actual_child_output_drains_in_bounded_chunks(tmp_path):
    size=512*1024
    process=child(f"import os;os.write(1,b'x'*{size})")
    writer=PipeLogWriter(process.stdout,tmp_path,threading.RLock())
    writer.start()
    try:
        process.wait(timeout=5)
        assert writer.finish()
        assert (tmp_path/'logs/llama.log').stat().st_size==size
        assert process.stdout.closed
    finally:
        stop_child(process)
        writer.finish()


def test_busy_delete_lock_discards_without_blocking_stdout_drain(tmp_path):
    data=b'x'*(256*1024)
    pipe=TrackedPipe(data)
    lock=threading.RLock()
    writer=PipeLogWriter(pipe,tmp_path,lock)
    with lock:
        writer.start()
        assert writer.finish(.2)
    assert pipe.total_read==len(data) and pipe.closed
    assert max(pipe.requests)==8192 and min(pipe.requests)>0
    assert not (tmp_path/'logs/llama.log').exists()


def test_path_or_write_failure_still_drains_all_chunks(tmp_path,monkeypatch):
    import engine.runtime_logging as module
    calls=[]
    def unavailable(*args,**kwargs):
        calls.append(True)
        raise OSError('synthetic unwritable log')
    monkeypatch.setattr(module,'regular_log_path',unavailable)
    data=b'a'*(160*1024)
    pipe=TrackedPipe(data)
    writer=PipeLogWriter(pipe,tmp_path,threading.RLock())
    writer.start()
    assert writer.finish()
    assert pipe.total_read==len(data) and pipe.closed and len(calls)>1
    assert writer.thread.daemon


def test_disk_append_error_is_isolated_from_pipe_reader(tmp_path,monkeypatch):
    import engine.runtime_logging as module
    class BadPath:
        def open(self,*args,**kwargs):
            raise PermissionError('synthetic sharing error')
    monkeypatch.setattr(module,'regular_log_path',lambda *a,**k:BadPath())
    pipe=TrackedPipe(b'one\ntwo\n',read_limit=2)
    writer=PipeLogWriter(pipe,tmp_path,threading.RLock())
    writer.start()
    assert writer.finish() and pipe.total_read==8 and pipe.closed


def test_local_key_redaction_crosses_chunk_boundaries_without_whole_buffer(tmp_path):
    secret='synthetic-local-key-1234567890'
    expected=b'first\n[REDACTED]\nlast\n'
    pipe=TrackedPipe(b'first\n'+secret.encode()+b'\nlast\n',read_limit=3)
    writer=PipeLogWriter(pipe,tmp_path,threading.RLock(),secrets=(secret,))
    writer.start()
    assert writer.finish()
    value=(tmp_path/'logs/llama.log').read_bytes()
    assert secret.encode() not in value and value==expected


def test_finish_is_bounded_even_if_an_idle_child_keeps_pipe_open(tmp_path):
    process=child("import time;print('ready',flush=True);time.sleep(60)")
    writer=PipeLogWriter(process.stdout,tmp_path,threading.RLock())
    writer.start()
    try:
        path=tmp_path/'logs/llama.log'
        wait_until(lambda:path.exists() and b'ready' in path.read_bytes())
        start=time.monotonic()
        assert writer.finish(.15)
        assert time.monotonic()-start<1
        assert process.stdout.closed and not writer.thread.is_alive()
    finally:
        stop_child(process)
        writer.finish()


def test_unstarted_writer_can_close_pipe(tmp_path):
    pipe=TrackedPipe(b'')
    writer=PipeLogWriter(pipe,tmp_path,threading.RLock())
    assert writer.finish() and pipe.closed


def test_invalid_log_directory_does_not_receive_output(tmp_path):
    (tmp_path/'logs').write_bytes(b'not a directory')
    pipe=TrackedPipe(b'private output')
    writer=PipeLogWriter(pipe,tmp_path,threading.RLock())
    writer.start()
    assert writer.finish() and pipe.closed
    assert (tmp_path/'logs').read_bytes()==b'not a directory'


def test_linked_log_file_is_not_appended(tmp_path):
    outside=tmp_path/'outside.txt'
    outside.write_bytes(b'unchanged')
    logs=tmp_path/'logs'
    logs.mkdir()
    try:
        (logs/'llama.log').symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation unavailable on this host')
    pipe=TrackedPipe(b'unwanted')
    writer=PipeLogWriter(pipe,tmp_path,threading.RLock())
    writer.start()
    assert writer.finish() and outside.read_bytes()==b'unchanged'


def test_hardlinked_log_file_is_not_appended(tmp_path):
    outside=tmp_path/'outside.txt'
    outside.write_bytes(b'unchanged')
    logs=tmp_path/'logs'
    logs.mkdir()
    os.link(outside,logs/'llama.log')
    writer=PipeLogWriter(TrackedPipe(b'unwanted'),tmp_path,threading.RLock())
    writer.start()
    assert writer.finish() and outside.read_bytes()==b'unchanged'
