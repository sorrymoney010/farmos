"""HTTP authorization regressions using real SQL queries on an isolated SQLite DB.

No production credentials or Postgres/Redis services are required. JWT validation
is outside this test: only identity lookup is overridden; role checks stay real.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.api.jobs import router
from app.db.models.core import Role, User, UserRole
from app.db.models.enums import RoleName
from app.db.models.jobs_ledger import Job, JobAssignment, JobLease, JobVerification
from app.db.session import Base, get_db
from app.dependencies import get_current_user


@pytest.fixture
def access(tmp_path):
    path = tmp_path / 'access.db'
    engine = create_engine(f'sqlite:///{path}')
    tables = [m.__table__ for m in (User, Role, UserRole, Job, JobAssignment, JobLease, JobVerification)]
    Base.metadata.create_all(engine, tables=tables)
    users = {name: User(id=uuid4(), email=f'{name}@example.test', password_hash='unused')
             for name in ('alice', 'bob', 'owner', 'admin', 'operations', 'provider')}
    now = datetime.now(timezone.utc)
    own = uuid4()
    other = uuid4()
    orphan = uuid4()
    with Session(engine) as db:
        db.add_all(users.values())
        roles = {name: Role(id=uuid4(), name=RoleName(name))
                 for name in ('CUSTOMER', 'OWNER', 'ADMIN', 'OPERATIONS', 'PROVIDER')}
        db.add_all(roles.values())
        for name, user in users.items():
            grants = ['CUSTOMER'] if name in ('alice', 'bob') else [name.upper()]
            if name == 'admin':
                grants.append('CUSTOMER')  # mixed-role staff must keep staff access
            for role in grants:
                db.add(UserRole(user_id=user.id, role_id=roles[role].id))
        db.add(Job(id=own, customer_id=users['alice'].id, adapter_id='internal_compute', workload_type='checksum',
                   requested_resources={'private': 'alice'}, created_at=now - timedelta(days=1)))
        db.add(Job(id=other, customer_id=users['bob'].id, adapter_id='internal_compute', workload_type='checksum',
                   requested_resources={'private': 'bob'}))
        db.add(Job(id=orphan, customer_id=None, adapter_id='internal_compute', workload_type='checksum'))
        # Other customers' recent work must not crowd out Alice before filtering.
        db.add_all(Job(customer_id=users['bob'].id, adapter_id='internal_compute', workload_type='checksum') for _ in range(101))
        db.commit()
        for user in users.values():
            db.refresh(user)
            db.expunge(user)
    engine.dispose()
    async_engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router, prefix='/jobs')

    async def identity(x_user: str = Header(default='alice')):
        return users[x_user]

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_current_user] = identity
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, own, other, orphan


def test_customer_reads_only_owned_jobs_before_pagination(access):
    client, own, other, orphan = access
    response = client.get('/jobs/')
    assert response.status_code == 200
    assert [row['job_id'] for row in response.json()] == [str(own)]
    assert client.get(f'/jobs/{own}').json()['payload'] == {'private': 'alice'}
    for job_id in (other, orphan, uuid4()):
        denied = client.get(f'/jobs/{job_id}')
        assert denied.status_code == 404
        assert denied.json() == {'detail': 'Job not found'}
    assert client.get(f'/jobs/{own}', headers={'X-User': 'bob'}).status_code == 404


@pytest.mark.parametrize('staff', ['owner', 'admin', 'operations'])
def test_staff_can_read_other_customer_jobs(access, staff):
    client, own, other, orphan = access
    headers = {'X-User': staff}
    assert len(client.get('/jobs/', headers=headers).json()) == 100
    for job_id in (own, other, orphan):
        assert client.get(f'/jobs/{job_id}', headers=headers).status_code == 200


@pytest.mark.parametrize('role', ['alice', 'bob', 'operations', 'provider'])
def test_legacy_create_rejects_non_admins(access, role):
    client, *_ = access
    response = client.post('/jobs/', headers={'X-User': role}, json={
        'workload_type': 'checksum', 'payload': {}, 'expected_revenue_usd': 100,
    })
    assert response.status_code == 403


@pytest.mark.parametrize('role', ['owner', 'admin'])
def test_internal_create_retains_admin_access(access, role):
    client, *_ = access
    # Invalid workload reaches handler validation, instead of failing role checks.
    response = client.post('/jobs/', headers={'X-User': role}, json={'workload_type': 'invalid'})
    assert response.status_code == 400
    assert 'Unsupported workload_type' in response.json()['detail']


def test_provider_cannot_read_jobs(access):
    client, own, *_ = access
    assert client.get('/jobs/', headers={'X-User': 'provider'}).status_code == 403
    assert client.get(f'/jobs/{own}', headers={'X-User': 'provider'}).status_code == 403
