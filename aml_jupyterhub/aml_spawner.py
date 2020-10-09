"""Create an AzureML Spawner for JupyterHub."""

import os
import time
import datetime
import re
import tempfile
import base64
import asyncio

from traitlets import Unicode, Integer, default, Bool
from jupyterhub.spawner import Spawner
from jupyterhub.crypto import decrypt

from async_generator import async_generator, yield_

from azureml.core import Workspace
from azureml.core.authentication import ServicePrincipalAuthentication
from azureml.core.compute import ComputeInstance
from azureml.exceptions import ComputeTargetException, ProjectSystemException

from . import redirector

URL_REGEX = re.compile(r'\bhttps://[^ ]*')
CODE_REGEX = re.compile(r'\b[A-Z0-9]{9}\b')


class AMLSpawner(Spawner):
    """
    A JupyterHub spawner that creates AzureML resources. A user will be given an
    AzureML workspace and an attached compute instance.

    "PanzureML" || "Panamel"

    """

    _vm_started_states = ["starting", "running"]
    _vm_transition_states = ["creating", "updating", "deleting"]
    _vm_stopped_states = ["stopping", "stopped"]
    _vm_bad_states = ["failed"]
    _events = None
    _last_progress = 50

    ip = Unicode('0.0.0.0', config=True,
                 help="The IP Address of the spawned JupyterLab instance.")

    start_timeout = Integer(
        3600, config=True,
        help="""
        Timeout (in seconds) before giving up on starting of single-user server.
        This is the timeout for start to return, not the timeout for the server to respond.
        Callers of spawner.start will assume that startup has failed if it takes longer than this.
        start should return when the server process is started and its location is known.
        """)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.workspace = None
        self.compute_instance = None
        self._application_urls = None
        self.redirect_server = None

        self.subscription_id = os.environ['SUBSCRIPTION_ID']
        self.location = os.environ['LOCATION']

        self.resource_group_name = os.environ['RESOURCE_GROUP']
        self.workspace_name = os.environ['SPAWN_TO_WORK_SPACE']
        self.compute_instance_name = self._make_safe_for_compute_name(
            self.user.escaped_name + os.environ['SPAWN_COMPUTE_INSTANCE_SUFFIX'])

        self.tenant_id = os.environ["AAD_TENANT_ID"]
        self.client_id = os.environ["AAD_CLIENT_ID"]
        self.client_secret = os.environ["AAD_CLIENT_SECRET"]

        self.sp_auth = ServicePrincipalAuthentication(
            tenant_id=self.tenant_id,
            service_principal_id=self.client_id,
            service_principal_password=self.client_secret)

    def _start_recording_events(self):
        self._events = []

    def _stop_recording_events(self):
        self._events = None

    def _add_event(self, msg, progress=None):
        if self._events is not None:
            if progress is None:
                progress = self._last_progress
            self._events.append((msg, progress))
            self.log.info(f"Event {msg}@{progress}%")
            self._last_progress = progress

    _VALID_MACHINE_NAME = re.compile(r"[A-z][-A-z0-9]{2,23}")

    def _make_safe_for_compute_name(self, name):
        name = re.sub('[^-0-9a-zA-Z]+', '', name)
        if not re.match('[A-z]', name[0]):
            name = 'A-' + name
        return name[:23]

    @ property
    def application_urls(self):
        if self._application_urls is None:
            if self.compute_instance is None:
                result = None
            else:
                result = self._applications()
            self.application_urls = result
        return self._application_urls

    @ application_urls.setter
    def application_urls(self, value):
        self._application_urls = value

    def _applications(self):
        """Parse Application URLs from the compute instance into a more queryable format."""
        applications = self.compute_instance.applications
        return {d["displayName"]: d["endpointUri"] for d in applications}

    def _poll_compute_setup(self):
        compute_instance_status = self.compute_instance.get_status()
        state = compute_instance_status.state
        errors = compute_instance_status.errors
        return state, errors

    def _get_workspace(self):
        try:
            self.workspace = Workspace(workspace_name=self.workspace_name,
                                       subscription_id=self.subscription_id,
                                       resource_group=self.resource_group_name,
                                       auth=self.sp_auth)
            self.log.info(f"Using workspace: {self.workspace_name}.")
            self._add_event(f"Using workspace: {self.workspace_name}.", 10)
        except ProjectSystemException:
            self.log.error(f"Workspace {self.workspace_name} not found!")
            self._add_event(f"Workspace {self.workspace_name} not found!", 1)
            raise

    def _set_up_compute_instance(self):
        """
        Set up an AML compute instance for the workspace. The compute instance is responsible
        for running the Python kernel and the optional JupyterLab instance for the workspace.
        """
        # Verify that cluster does not exist already.
        try:
            self.compute_instance = ComputeInstance(workspace=self.workspace,
                                                    name=self.compute_instance_name)

            self.log.info(f"Compute instance {self.compute_instance_name} already exists.")
            self._add_event(f"Compute instance {self.compute_instance_name} already exists", 20)
        except ComputeTargetException:
            self._add_event(f"Creating compute instance {self.compute_instance_name}", 15)
            instance_config = ComputeInstance.provisioning_configuration(vm_size="Standard_DS1_v2",
                                                                        #  ssh_public_access=True,
                                                                        #  admin_user_ssh_public_key=os.environ.get('SSH_PUB_KEY'),
                                                                         assigned_user_object_id=self.environment['USER_OID'],
                                                                         assigned_user_tenant_id=self.tenant_id)
            self.compute_instance = ComputeInstance.create(self.workspace,
                                                         self.compute_instance_name,
                                                         instance_config)
            self.log.info(f"Created compute instance {self.compute_instance_name}.")
            self._add_event(f"Created compute instance {self.compute_instance_name}.", 20)

    def _start_compute_instance(self):
        stopped_state = "stopped"
        state, _ = self._poll_compute_setup()
        self.log.info(f"Compute instance state is {state}.")
        self._add_event(f"Compute instance in {state} state.", 20)

        if state.lower() == stopped_state:
            try:
                self.log.info(f"Starting the compute instance.")
                self._add_event("Starting the compute instance. This may take a short while...", 25)
                self.compute_instance.start()
            except ComputeTargetException as e:
                self.log.warning(f"Could not start compute resource:\n{e.message}.")

    def _stop_compute_instance(self):
        try:
            self.log.info(f"Stopping the compute instance.")
            self.compute_instance.stop()

        except ComputeTargetException as e:
            self.log.warning(e.message)

    async def _wait_for_target_state(self, target_state, progress_between=(30, 70), progress_in_seconds=240):
        """ Wait for the compute instance to be in the target state.

        emit events reporting progress starting at `progress_between[0]` to `progress_between[1]` over `progress_in_seconds` seconds.
        This is to give the use watching the progress bar the illusion of progress even if we don't really know how far we have progressed.
        """
        started_at = datetime.datetime.now()
        while True:
            state, _ = self._poll_compute_setup()
            time_taken = datetime.datetime.now() - started_at
            min_progress, max_progress = progress_between
            progress = (min_progress + (max_progress - min_progress) * (time_taken.total_seconds()/progress_in_seconds))//1
            progress = max_progress if progress > max_progress else progress
            if state.lower() == target_state:
                self.log.info(f"Compute in target state {target_state}.")
                self._add_event(f"Compute in target state '{target_state}'.", max_progress)
                break
            elif state.lower() in self._vm_bad_states:
                self._add_event(f"Compute instance in failed state: {state!r}.", min_progress)
                raise ComputeTargetException(f"Compute instance in failed state: {state!r}.")
            else:
                self._add_event(
                    f"Compute in state '{state.lower()}' after {time_taken.total_seconds():.0f} seconds."
                    + f"Aiming for target state '{target_state}', this may take a short while", progress)
            await asyncio.sleep(5)

    def _stop_redirect(self):
        if self.redirect_server:
            self.log.info(f"Stopping the redirect server route: {self.redirect_server.route}.")
            self.redirect_server.stop()
            self.redirect_server = None

    async def _set_up_resources(self):
        """Both of these methods are blocking, so try and async them as a pair."""
        self._get_workspace()
        self._set_up_compute_instance()
        self._start_compute_instance()  # Ensure existing but stopped resources are running.

    def _tear_down_resources(self):
        """This method blocks, so try and async it and pass back to a checker."""
        self._stop_compute_instance()
        self._stop_redirect()

    def get_url(self):
        """An AzureML compute instance knows how to get its JupyterLab instance URL, so expose it."""
        key = "Jupyter Lab"
        return None if self.application_urls is None else self.application_urls[key]

    @ async_generator
    async def progress(self):
        while self._events is not None:
            if len(self._events) > 0:
                msg, progress = self._events.pop(0)
                await yield_({
                    'progress': progress,
                    'message':  msg
                })
            await asyncio.sleep(1)

    async def start(self):
        """Start (spawn) AzureML resouces."""
        try:
            self._start_recording_events()
            self._add_event("Initializing...", 0)

            auth_state = await decrypt(self.user.encrypted_auth_state)
            self.environment['USER_OID'] = auth_state["user"]["oid"]

            await self._set_up_resources()

            target_state = "running"
            await self._wait_for_target_state(target_state)

            url = self.application_urls["Jupyter Lab"]
            route = redirector.RedirectServer.get_existing_redirect(url)
            if route:
                self._add_event(f"Existing route to compute instance found.", 95)
            else:
                self._add_event(f"Creating route to compute instance.", 91)
                self.redirect_server = redirector.RedirectServer(url)
                self.redirect_server.start()
                await asyncio.sleep(1)
                route = self.redirect_server.route
                self._add_event(f"Route to compute instance created.", 95)

            self._add_event(f"Set up complete. Prepare for redirect...", 100)

            return route
        finally:
            self._stop_recording_events()

    async def stop(self, now=False):
        """Stop and terminate all spawned AzureML resources."""
        self._tear_down_resources()

        self._stop_redirect()

        if not now:
            target_state = "stopped"
            await self._wait_for_target_state(target_state)

    async def poll(self):
        """
        Healthcheck of spawned AzureML resources.

        Checked statuses are as follows:
          * None: resources are running or starting up
          * 0 if unknown exit status
          * int > 0 for known exit status:
              * 1: Known error returned by polling the instance
              * 2: Compute instance found in an unhealthy state
              * 3: Compute instance stopped

        """
        result = None
        if self.compute_instance is not None:
            status, errors = self._poll_compute_setup()
            if status.lower() not in self._vm_started_states:
                if status.lower() in self._vm_stopped_states:
                    # Assign code 3 == instance stopped.
                    result = 3
                elif status.lower() in self._vm_bad_states:
                    # Assign code 2 == instance bad.
                    result = 2
                elif len(errors):
                    # Known error.
                    result = 1
                else:
                    # Something else.
                    result = 0
        else:
            # Compute has not started, so treat as if not running.
            result = 0
        return result

    # def get_state(self):
    #     """Get the state of our spawned AzureML resources so that we can persist over restarts."""
    #     state = super().get_state()
    #     state["workspace_name"] = self.workspace_name
    #     state["compute_instance_name"] = self.compute_instance_name
    #     return state

    # def load_state(self, state):
    #     """Load previously-defined state so that we can resume where we left off."""
    #     super().load_state(state)
    #     if "workspace_name" in state:
    #         self.workspace_name = state["workspace_name"]
    #     if "compute_instance_name" in state:
    #         self.compute_instance_name = state["compute_instance_name"]
