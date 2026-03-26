web: gunicorn app:app --worker-class gevent --workers 1 --worker-connections 100 --timeout 120
worker: python scheduler.py
