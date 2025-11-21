# Training Creator - Deployment Guide

This guide covers deploying the Training Creator web application for your SaaS MVP.

## Table of Contents
1. [Local Development](#local-development)
2. [Production Deployment](#production-deployment)
3. [Cloud Platforms](#cloud-platforms)
4. [Environment Configuration](#environment-configuration)
5. [Scaling Considerations](#scaling-considerations)

## Local Development

### Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the development server
python app.py

# Access the application at:
# http://localhost:5000
```

### Development Features
- Auto-reload on code changes
- Debug mode enabled
- Detailed error messages
- No authentication required

## Production Deployment

### Option 1: Gunicorn (Recommended for Production)

```bash
# Install Gunicorn
pip install gunicorn

# Run with Gunicorn
gunicorn -w 4 -b 0.0.0.0:8000 app:app

# With more configuration:
gunicorn -w 4 \
  --timeout 120 \
  --max-requests 1000 \
  --max-requests-jitter 50 \
  -b 0.0.0.0:8000 \
  app:app
```

### Option 2: Docker

```bash
# Build the Docker image
docker build -t training-creator .

# Run the container
docker run -p 8000:8000 \
  -v $(pwd)/uploads:/app/uploads \
  -v $(pwd)/outputs:/app/outputs \
  training-creator
```

### Option 3: systemd Service (Linux)

Create `/etc/systemd/system/training-creator.service`:

```ini
[Unit]
Description=Training Creator Web Application
After=network.target

[Service]
Type=notify
User=www-data
WorkingDirectory=/opt/training-creator
Environment="PATH=/opt/training-creator/venv/bin"
ExecStart=/opt/training-creator/venv/bin/gunicorn -w 4 -b 0.0.0.0:8000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start the service
sudo systemctl enable training-creator
sudo systemctl start training-creator
sudo systemctl status training-creator
```

## Cloud Platforms

### Deploying to Heroku

```bash
# Login to Heroku
heroku login

# Create new app
heroku create your-training-creator-app

# Add buildpack
heroku buildpacks:set heroku/python

# Deploy
git push heroku main

# Scale dynos
heroku ps:scale web=1
```

**Procfile** (create in root directory):
```
web: gunicorn -w 4 --timeout 120 app:app
```

### Deploying to AWS Elastic Beanstalk

```bash
# Install EB CLI
pip install awsebcli

# Initialize EB
eb init -p python-3.11 training-creator

# Create environment
eb create training-creator-env

# Deploy
eb deploy

# Open in browser
eb open
```

### Deploying to Google Cloud Run

```bash
# Build and deploy
gcloud run deploy training-creator \
  --source . \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated
```

### Deploying to DigitalOcean App Platform

1. Connect your GitHub repository
2. Select the branch to deploy
3. Set build command: `pip install -r requirements.txt`
4. Set run command: `gunicorn -w 4 -b 0.0.0.0:8080 app:app`
5. Deploy

### Deploying to Azure Web Apps

```bash
# Create resource group
az group create --name TrainingCreatorRG --location eastus

# Create app service plan
az appservice plan create --name TrainingCreatorPlan \
  --resource-group TrainingCreatorRG --sku B1 --is-linux

# Create web app
az webapp create --resource-group TrainingCreatorRG \
  --plan TrainingCreatorPlan --name your-training-creator \
  --runtime "PYTHON:3.11"

# Deploy code
az webapp up --name your-training-creator
```

## Environment Configuration

### Production Environment Variables

Create a `.env` file or set environment variables:

```bash
# Flask configuration
FLASK_ENV=production
SECRET_KEY=your-very-secure-random-secret-key-here
MAX_CONTENT_LENGTH=16777216  # 16MB

# File storage
UPLOAD_FOLDER=/var/data/uploads
OUTPUT_FOLDER=/var/data/outputs

# Optional: Database for user accounts (future)
DATABASE_URL=postgresql://user:pass@localhost/trainingcreator

# Optional: Redis for session storage (future)
REDIS_URL=redis://localhost:6379

# Optional: Cloud storage (future)
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_S3_BUCKET=training-packages

# Monitoring (optional)
SENTRY_DSN=your-sentry-dsn
```

### Updating app.py for Production

For production, update these lines in `app.py`:

```python
import os

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['OUTPUT_FOLDER'] = os.environ.get('OUTPUT_FOLDER', 'outputs')

# Don't use debug mode in production!
if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_ENV') == 'development')
```

## Scaling Considerations

### File Storage

For SaaS, you'll need persistent storage:

**Option 1: AWS S3**
```python
import boto3

s3_client = boto3.client('s3')
s3_client.upload_file(local_path, bucket, s3_path)
```

**Option 2: Azure Blob Storage**
```python
from azure.storage.blob import BlobServiceClient

blob_service = BlobServiceClient.from_connection_string(conn_str)
blob_client = blob_service.get_blob_client(container, blob_name)
blob_client.upload_blob(data)
```

### Background Processing

For long-running tasks, use Celery:

```bash
pip install celery redis
```

```python
from celery import Celery

celery = Celery('tasks', broker='redis://localhost:6379')

@celery.task
def process_sop_async(file_path, params):
    # Your processing code here
    pass
```

### Database for User Management

Add PostgreSQL for user accounts:

```python
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
db = SQLAlchemy(app)
login_manager = LoginManager(app)
```

### Caching

Add Redis caching for frequently accessed data:

```python
from flask_caching import Cache

cache = Cache(app, config={'CACHE_TYPE': 'redis', 'CACHE_REDIS_URL': 'redis://localhost:6379'})

@cache.cached(timeout=300)
def expensive_operation():
    pass
```

### Rate Limiting

Protect your API from abuse:

```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app,
    key_func=get_remote_address,
    default_limits=["100 per hour"]
)

@app.route('/api/upload', methods=['POST'])
@limiter.limit("10 per minute")
def upload_file():
    pass
```

## Monitoring and Maintenance

### Health Checks

The application includes a `/health` endpoint for monitoring:

```bash
curl https://your-app.com/health
# Response: {"status": "healthy", "version": "1.0.0"}
```

### Log Management

For production, use structured logging:

```python
import logging
from logging.handlers import RotatingFileHandler

handler = RotatingFileHandler('app.log', maxBytes=10000000, backupCount=3)
handler.setLevel(logging.INFO)
app.logger.addHandler(handler)
```

### Error Tracking

Integrate Sentry for error tracking:

```python
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

sentry_sdk.init(
    dsn=os.environ.get('SENTRY_DSN'),
    integrations=[FlaskIntegration()]
)
```

### Backups

Regularly backup user data and generated packages:

```bash
# Backup uploads and outputs
tar -czf backup-$(date +%Y%m%d).tar.gz uploads/ outputs/

# Upload to S3
aws s3 cp backup-$(date +%Y%m%d).tar.gz s3://your-backup-bucket/
```

## Security Best Practices

1. **Use HTTPS**: Always use SSL/TLS in production
2. **Secure Secret Key**: Use a strong, random secret key
3. **File Validation**: Validate all uploaded files
4. **Size Limits**: Enforce file size limits
5. **CORS**: Configure CORS properly if needed
6. **Authentication**: Add user authentication for production
7. **Input Sanitization**: Sanitize all user inputs
8. **Regular Updates**: Keep dependencies updated

```bash
# Check for security vulnerabilities
pip install safety
safety check

# Update dependencies
pip list --outdated
```

## Performance Optimization

1. **Use CDN**: Serve static files from a CDN
2. **Enable Gzip**: Compress responses
3. **Optimize Images**: Minimize asset sizes
4. **Database Indexing**: Index frequently queried fields
5. **Async Processing**: Use background tasks for heavy operations
6. **Caching**: Cache expensive operations

## Cost Estimation (Monthly)

### Small Scale (1-100 users)
- **Heroku**: $7-25/month
- **DigitalOcean**: $12/month (Basic Droplet)
- **AWS**: $15-30/month (t3.micro + S3)

### Medium Scale (100-1000 users)
- **AWS**: $50-150/month (t3.small + RDS + S3)
- **Azure**: $60-200/month
- **Google Cloud**: $50-150/month

### Large Scale (1000+ users)
- **AWS**: $200-1000+/month
- **Multi-region deployment**
- **Load balancing**
- **Auto-scaling**

## Next Steps for SaaS

1. **User Authentication**: Add login/signup
2. **Payment Integration**: Stripe or PayPal
3. **Usage Analytics**: Track user activity
4. **Email Notifications**: Send completion emails
5. **API Keys**: For programmatic access
6. **White-labeling**: Custom branding
7. **Team Collaboration**: Multi-user accounts
8. **Version History**: Track package versions
9. **Templates**: Pre-built SOP templates
10. **Reporting**: Usage and analytics dashboard
