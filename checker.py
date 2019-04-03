import argparse
import collections
import json
from jsonpointer import resolve_pointer
import subprocess

from googleapiclient import discovery
from oauth2client.client import GoogleCredentials


parser = argparse.ArgumentParser()
parser.add_argument('terraform_dir')


def main():
    parsed_args = parser.parse_args()

    print('Fetching terraform state...')
    result = subprocess.run(
        ["terraform", "state", "pull"],
        check=True,
        capture_output=True,
        cwd=parsed_args.terraform_dir)
    state = json.loads(result.stdout)
    print('Got terraform state')

    credentials = GoogleCredentials.get_application_default()
    service_v1 = discovery.build('cloudresourcemanager', 'v1', credentials=credentials)
    service_v2 = discovery.build('cloudresourcemanager', 'v2', credentials=credentials)

    for module in state['modules']:
        check_org_iam(service_v1, module)
        check_folders(service_v2, module)
        check_folder_iam(service_v2, module)
        check_dns(credentials, module)


def check_dns(credentials, state):
    service = discovery.build('dns', 'v1', credentials=credentials)

    project_states = [
        resource for resource in state['resources'].values()
        if resource['type'] == 'google_project'
    ]

    gcp_recordsets = set()
    for project in project_states:
        project_id = resolve_pointer(project, '/primary/id')

        request = service.managedZones().list(project=project_id)
        while request is not None:
            response = request.execute()

            for managed_zone in response['managedZones']:
                managed_zone_name = managed_zone["name"]
                for record_value, record_type in _get_recordsets_for_zone(service, project_id, managed_zone_name):
                    gcp_recordsets.add((project_id, managed_zone_name, record_value, record_type))

            request = service.managedZones().list_next(previous_request=request, previous_response=response)

    dns_rs_states = set(
        (
            resolve_pointer(resource, '/primary/attributes/project'),
            resolve_pointer(resource, '/primary/attributes/managed_zone'),
            resolve_pointer(resource, '/primary/attributes/name'),
            resolve_pointer(resource, '/primary/attributes/type'),
        )
        for resource in state['resources'].values()
        if resource['type'] == 'google_dns_record_set'
    )

    missing_dns = gcp_recordsets.difference(dns_rs_states)

    if missing_dns:
        print(f'\nTerraform is not controlling DNS records:')

        for missing_rs in missing_dns:
            project_id, zone, name, rs_type = missing_rs
            print(f'\t{name} ({rs_type} record)\n\t\tin managed zone {zone} of project {project_id}')


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
        print(f'\nTerraform is not controlling folders:')
    
        for missing_folder_id in missing_folder_ids:
            folder_data = gcp_folders[missing_folder_id]
            print(f'\t{folder_data["displayName"]} ({folder_data["name"]})')


def check_org_iam(service, state):
    org_state = next(
        resource for resource in state['resources'].values()
        if resource['type'] == 'google_organization'
    )

    request = service.organizations().getIamPolicy(
        resource=resolve_pointer(org_state, '/primary/attributes/name'))
    response = request.execute()

    gcp_iam_ids = set()
    for binding in response['bindings']:
        for member in binding['members']:
            gcp_iam_ids.add((member, binding['role'],))

    state_iam_ids = set(
        (
            resolve_pointer(resource, '/primary/attributes/member'),
            resolve_pointer(resource, '/primary/attributes/role'),
        )
        for resource in state['resources'].values()
        if resource['type'] == 'google_organization_iam_member'
    )

    missing_iam_ids = gcp_iam_ids.difference(state_iam_ids)

    if missing_iam_ids:
        org_name = resolve_pointer(org_state, '/primary/attributes/domain')
        print(f'\nTerraform is not controlling IAM bindings for the {org_name} organisation:')
    
        for missing_iam_id in missing_iam_ids:
            member, role = missing_iam_id
            print(f'\t{member}: {role}')


def check_folder_iam(service, state):
    folder_states = {
        resolve_pointer(resource, '/primary/attributes/id'): resource
        for resource in state['resources'].values()
        if resource['type'] == 'google_folder'
    }

    iam_states_by_folder_name = collections.defaultdict(list)
    for resource in state['resources'].values():
        if resource['type'] == 'google_folder_iam_member':
            folder_name = resolve_pointer(resource, '/primary/attributes/folder')
            iam_states_by_folder_name[folder_name].append(resource)

    for folder_name, folder_resources in iam_states_by_folder_name.items():
        request = service.folders().getIamPolicy(resource=folder_name)
        response = request.execute()

        gcp_iam_ids = set()
        for binding in response['bindings']:
            for member in binding['members']:
                gcp_iam_ids.add((member, binding['role'],))

        state_iam_ids = set(
            (
                resolve_pointer(resource, '/primary/attributes/member'),
                resolve_pointer(resource, '/primary/attributes/role'),
            )
            for resource in folder_resources
        )

        missing_iam_ids = gcp_iam_ids.difference(state_iam_ids)

        if missing_iam_ids:
            folder_display_name = resolve_pointer(
                folder_states[folder_name], '/primary/attributes/display_name')
            print(
                '\nTerraform is not controlling IAM bindings for folder '
                f'{folder_display_name} ({folder_name})')

            for missing_iam_id in missing_iam_ids:
                member, role = missing_iam_id
                print(f'\t{member}: {role}')


def _get_recordsets_for_zone(service, project_id, zone_name):
    recordsets = []
    request = service.resourceRecordSets().list(
        project=project_id, managedZone=zone_name)
    while request is not None:
        response = request.execute()

        recordsets += [
            (resource["name"], resource["type"],) for resource in response['rrsets']
        ]

        request = service.resourceRecordSets().list_next(previous_request=request, previous_response=response)

    return recordsets


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
