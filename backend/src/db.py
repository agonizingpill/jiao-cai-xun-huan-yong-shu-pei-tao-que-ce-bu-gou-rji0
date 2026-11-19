import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase,sessionmaker
url=os.environ['DATABASE_URL']
engine=create_engine(url,pool_pre_ping=True,**({}if url.startswith('sqlite')else{'isolation_level':'READ COMMITTED'}))
Session=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
def session():
    with Session()as s:yield s
