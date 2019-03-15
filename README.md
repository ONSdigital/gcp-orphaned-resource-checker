# Google Cloud Platform Orphaned Resource Checker

Tool to identify resources in GCP that aren't managed by terraform.

# Prerequisites

- Python 3.6+
- Google Cloud SDK

## Basic usage

### Dependendies

```
pyenv install
pip install --upgrade pip setuptools pipenv
pipenv install
```

### Running

```
gcloud auth login
pipenv run python checker.py <my terraform directory>
```
