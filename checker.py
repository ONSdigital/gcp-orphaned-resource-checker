import argparse
import collections
import json
from jsonpointer import resolve_pointer
import subprocess

from googleapiclient import discovery
from oauth2client.client import GoogleCredentials


parser = argparse.ArgumentParser(
    description="Tool to identify resources in GCP that aren't managed by terraform.")
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

    for module in state['modules']:
        resources = collections.defaultdict(list)
        for resource in module['resources'].values():
            resources[resource.get('type')].append(resource)

        check_org_iam(credentials, resources)
        check_folders(credentials, resources)
        check_folder_iam(credentials, resources)
        check_dns(credentials, resources)


def check_dns(credentials, resources):
    """
    Prints any non-terraformed recordsets that belong to a managed
    zone that was created by terraform
    """
    service = discovery.build('dns', 'v1', credentials=credentials)

    project_states = list(resources['google_project'])

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
        for resource in resources['google_dns_record_set']
    )

    missing_dns = gcp_recordsets.difference(dns_rs_states)

    if missing_dns:
        print(f'\nTerraform is not controlling DNS records:')

        for missing_rs in missing_dns:
            project_id, zone, name, rs_type = missing_rs
            print(f'\t{name} ({rs_type} record)\n\t\tin managed zone {zone} of project {project_id}')


def check_folders(credentials, resources):
    """
    Prints any non-terraformed folders which are siblings of terraformed folders
    """
    service = discovery.build('cloudresourcemanager', 'v2', credentials=credentials)

    folder_states = resources['google_folder']

    parent_ids = [
        resolve_pointer(folder, '/primary/attributes/parent')
        for folder in folder_states
    ]
    parent_ids = set(filter(None, parent_ids))

    gcp_folders = {}
    for parent_id in parent_ids:
        gcp_folders.update(_get_gcp_folders_in_parent(service, parent_id))

    state_folders = set(
        resolve_pointer(folder, '/primary/attributes/name')
        for folder in folder_states
    )
    gcp_folder_ids = set(gcp_folders.keys())
    missing_folder_ids = gcp_folder_ids.difference(state_folders)

    if missing_folder_ids:
        print(f'\nTerraform is not controlling folders:')
    
        for missing_folder_id in missing_folder_ids:
            folder_data = gcp_folders[missing_folder_id]
            print(f'\t{folder_data["displayName"]} ({folder_data["name"]})')


def check_org_iam(credentials, resources):
    """
    Prints any non-terraformed IAM bindings defined on the organisation
    """
    service = discovery.build('cloudresourcemanager', 'v1', credentials=credentials)

    org_state = next(iter(resources['google_organization']))

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
        for resource in resources['google_organization_iam_member']
    )

    missing_iam_ids = gcp_iam_ids.difference(state_iam_ids)

    if missing_iam_ids:
        org_name = resolve_pointer(org_state, '/primary/attributes/domain')
        print(f'\nTerraform is not controlling IAM bindings for the {org_name} organisation:')
    
        for missing_iam_id in missing_iam_ids:
            member, role = missing_iam_id
            print(f'\t{member}: {role}')


def check_folder_iam(credentials, resources):
    """
    Prints any non-terraformed IAM bindings on any terraformed folder
    """
    service = discovery.build('cloudresourcemanager', 'v2', credentials=credentials)

    folder_states = {
        resolve_pointer(resource, '/primary/attributes/id'): resource
        for resource in resources['google_folder']
    }

    iam_states_by_folder_name = collections.defaultdict(list)
    for member_resource in resources['google_folder_iam_member']:
        folder_name = resolve_pointer(member_resource, '/primary/attributes/folder')
        iam_states_by_folder_name[folder_name].append(member_resource)

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
