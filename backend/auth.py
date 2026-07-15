from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
import os

router = APIRouter()

# Read from env or config in production
CLIENT_ID = os.getenv("TAPIS_CLIENT_ID", "tapisui-implicit-client")
TAPIS_BASE_URL = os.getenv("TAPIS_BASE_URL", "https://icicleai.tapis.io")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8002")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

@router.get("/login")
def login(request: Request):
    """
    Mocked Local Login Flow
    Bypasses the strict Tapis Authorization server entirely to allow local UI development.
    Directly sets the cookies using the known token from tapis_api_examples.py.
    """
    # Hardcoded token from tapis_api_examples.py
    simulated_jwt = "eyJhbGciOiJSUzI1NiIsImtpZCI6IlBiZU5IU3lJVGtZRHctOWtnbjRZU21VSnk2ZVRYZTNEYWFMRDNBZnl0SDQiLCJ0eXAiOiJKV1QifQ.eyJqdGkiOiI4NDFiMGY4Ni1jNGQzLTQxZDUtOTM3My1jM2I0ZDAzODBjMzciLCJpc3MiOiJodHRwczovL2ljaWNsZWFpLnRhcGlzLmlvL3YzL3Rva2VucyIsInN1YiI6ImFydW5hY2hhbGFtLjMxQG9zdS5lZHVAaWNpY2xlYWkiLCJ0YXBpcy90ZW5hbnRfaWQiOiJpY2ljbGVhaSIsInRhcGlzL3Rva2VuX3R5cGUiOiJhY2Nlc3MiLCJ0YXBpcy9kZWxlZ2F0aW9uIjpmYWxzZSwidGFwaXMvZGVsZWdhdGlvbl9zdWIiOm51bGwsInRhcGlzL3VzZXJuYW1lIjoiYXJ1bmFjaGFsYW0uMzFAb3N1LmVkdSIsInRhcGlzL2FjY291bnRfdHlwZSI6InVzZXIiLCJleHAiOjE3Nzk5MjU0NjIsInRhcGlzL2NsaWVudF9pZCI6InRhcGlzdWktaW1wbGljaXQtY2xpZW50IiwidGFwaXMvZ3JhbnRfdHlwZSI6ImltcGxpY2l0IiwidGFwaXMvaWRwX2lkIjoiZ2xvYnVzIn0.T-53ohnf-xg5uWRIXEv_vV83GcIZ-fxmn2lM-zk-PhBqfV3KFHdVL-qyuy2mZayXfk8WEjVsOkEkYiWyaZvcMI3RcDMnBypit7nVG06-InxE7u5e0dUQ9Gy0uy4m33PjZ2OXqXJmqHXC0xfULRLQM9WDtsR-lVUbzF4UwYJkNgho_njJhDb462R7tmF6PYf4kFnl7Fr5XyDQjJG3l2clWASsISsK8o5r7otyaK3gPzgznUBV4v3vepZ2VZhc0VGZ2sj58rt3CU3-ZvjPy3FR2yQ_2U2vM9zvstdsnivnRGA7e0DaqM-uzrIuNPmbd1tfTt-7qImRhZp-OKxBxbuj_Q"
    simulated_username = "arunachalam.31@osu.edu"

    # We redirect directly back to the frontend URL
    redirect_response = RedirectResponse(url=FRONTEND_URL)
    
    # Set cookies just like harvest-webservers did
    redirect_response.set_cookie(key="token", value=simulated_jwt, httponly=False)
    redirect_response.set_cookie(key="username", value=simulated_username, httponly=False)

    return redirect_response

@router.get("/oauth2/callback")
def callback(code: str, response: Response):
    """
    Process the callback from Tapis.
    1. Exchange 'code' for a JWT token (simulated here for simplicity, requires httpx in prod)
    2. Set the token as a cookie
    3. Redirect to the frontend
    """
    
    # [!] PRODUCTION TODO: 
    # Use httpx to POST to Tapis /v3/oauth2/tokens to exchange `code` for the actual `jwt`
    # response = httpx.post(f"{TAPIS_BASE_URL}/v3/oauth2/tokens", data={...})
    
    # For now, we simulate success to allow local testing
    simulated_jwt = "simulated_tapis_jwt_token_12345"
    simulated_username = "testuser"

    # We redirect to the frontend URL
    redirect_response = RedirectResponse(url=FRONTEND_URL)
    
    # Set cookies just like harvest-webservers did
    redirect_response.set_cookie(key="token", value=simulated_jwt, httponly=False) # httponly=False so frontend can read it if needed
    redirect_response.set_cookie(key="username", value=simulated_username, httponly=False)

    return redirect_response
