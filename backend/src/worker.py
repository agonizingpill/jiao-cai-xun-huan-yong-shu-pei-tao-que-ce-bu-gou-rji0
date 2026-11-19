import time,logging
from pathlib import Path
from .db import Session
from . import dp_service

def recover():
    with Session()as s:
        done=dp_service.recover(s)
        if done:logging.info('recovered DP releases %s',done)

def loop():
    while True:
        Path('/tmp/books-heartbeat').touch()
        try:recover()
        except Exception:logging.exception('recovery loop')
        time.sleep(2)

if __name__=='__main__':recover();loop()
