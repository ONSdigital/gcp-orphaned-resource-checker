import argparse
import json
from jsonpointer import resolve_pointer
import subprocess

from googleapiclient import discovery
from oauth2client.client import GoogleCredentials


parser = argparse.ArgumentParser()
parser.add_argument('terraform_dir')


def main():
    parsed_args = parser.parse_args()

    result = subprocess.run(
        ["terraform", "state", "pull"],
        check=True,
        capture_output=True,
        cwd=parsed_args.terraform_dir)
    state = json.loads(result.stdout)

    credentials = GoogleCredentials.get_application_default()
    service = discovery.build('cloudresourcemanager', 'v2', credentials=credentials)

    for module in state['modules']:
        check_folders(service, module)


def check_folders(service, state):
    folder_states = {
        key: resource for key, resource in state['resources'].items()
        if resource['type'] == 'google_folder'
    }

    parent_ids = [
        resolve_pointer(folder, '/primary/attributes/parent')
        for folder in folder_states.values()
    ]
    parent_ids = set(filter(None, parent_ids))

    gcp_folders = {}
    for parent_id in parent_ids:
        gcp_folders.update(_get_gcp_folders_in_parent(service, parent_id))

    state_folders = set(
        resolve_pointer(folder, '/primary/attributes/name')
        for folder in folder_states.values()
    )
    gcp_folder_ids = set(gcp_folders.keys())
    missing_folder_ids = gcp_folder_ids.difference(state_folders)

    if missing_folder_ids:
        print(f'Terraform is not controlling folders:')
    
        for missing_folder_id in missing_folder_ids:
            print(f'\t{gcp_folders[missing_folder_id]}')


def _get_gcp_folders_in_parent(service, parent_id):
    request = service.folders().list(parent=parent_id)
    response = request.execute()

    gcp_folders = {
        folder['name']: folder for folder in
        response.get('folders', [])
    }

    while True:
        request = service.folders().list_next(previous_request=request, previous_response=response)
        if request:
            response = request.execute()
            gcp_folders.update({
                folder['name']: folder for folder in
                response.get('folders', [])
            })            
        else:
            return gcp_folders


if __name__ == '__main__':
    main()
