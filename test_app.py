import pytest
from app import app

@pytest.fixture
def client():
    return app.test_client()

def test_home_renders_index(client):
    response = client.get('/')
    assert response.status_code == 200
    assert "text/html" in response.content_type
