## About

Code work and experiments to integrate Azure Machine Learning with JupyterHub.

`files.py` - Experiment(s) for testing and exploring using Azure File Shares to provide users storage that is private and persistent across instance and workspaces.

`aml_spawner.py` - A JupyterHub spawner that spawns compute instances on Azure Machine Learning.

## How to develop

### Set up your environment vars
copy `env.template` to `.env` and fill in.

Ensure these variables are available when running.

This is the default for a `.env` file in many IDEs but else you could folow one of the suggestions [here](https://gist.github.com/mihow/9c7f559807069a03e302605691f85572) such as `set -o allexport; source .env; set +o allexport`


### Set up you conda environment

`conda create --from-file env.yaml`

### Up date the env

If you update the environment (install packages etc) then update the record of it.

`conda env export > env.yaml`