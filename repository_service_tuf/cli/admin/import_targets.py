import json
import os
from datetime import datetime
from typing import Any, Dict, List

from rich.console import Console
from sqlalchemy import Connection, MetaData, Table, create_engine
from sqlalchemy.exc import IntegrityError

from repository_service_tuf.cli import click
from repository_service_tuf.cli.admin import admin
from repository_service_tuf.helpers.api_client import (
    URL,
    Methods,
    get_headers,
    is_logged,
    request_server,
    task_status,
)
from repository_service_tuf.helpers.tuf import Metadata, SuccinctRoles

console = Console()


def _check_csv_files(csv_files: List[str]):
    not_found_csv_files: List[str] = []
    for csv_file in csv_files:
        if not os.path.isfile(csv_file):
            not_found_csv_files.append(csv_file)

    if len(not_found_csv_files) > 0:
        raise click.ClickException(
            f"CSV file(s) not found: {(', ').join(not_found_csv_files)}"
        )


def _parse_csv_data(
    csv_file: str, succinct_roles: SuccinctRoles
) -> List[Dict[str, Any]]:
    rstuf_db_data: List[Dict[str, Any]] = []
    with open(csv_file, "r") as f:
        for line in f:
            rstuf_db_data.append(
                {
                    "path": line.split(";")[0],
                    "info": {
                        "length": int(line.split(";")[1]),
                        "hashes": {line.split(";")[2]: line.split(";")[3]},
                    },
                    "rolename": succinct_roles.get_role_for_target(
                        line.split(";")[0]
                    ),
                    "published": False,
                    "action": "ADD",
                    "last_update": datetime.now(),
                }
            )

    return rstuf_db_data


def _import_csv_to_rstuf(
    db_client: Connection,
    rstuf_table: Table,
    csv_files: List[str],
    succinct_roles: SuccinctRoles,
) -> None:
    for csv_file in csv_files:
        console.print(f"Import status: Loading data from {csv_file}")
        rstuf_db_data = _parse_csv_data(csv_file, succinct_roles)
        console.print(f"Import status: Importing {csv_file} data")
        try:
            db_client.execute(rstuf_table.insert(), rstuf_db_data)
        except IntegrityError:
            raise click.ClickException(
                "Import status: ABORTED due duplicated targets. "
                "CSV files must to have unique targets (path). "
                "No data added to RSTUF DB."
            )
        console.print(f"Import status: {csv_file} imported")


@admin.command()
@click.option(
    "-metadata-url",
    required=True,
    help="RSTUF Metadata URL i.e.: http://127.0.0.1 .",
)
@click.option(
    "-db-uri",
    required=True,
    help="RSTUF DB URI. i.e.: postgresql://postgres:secret@127.0.0.1:5433",
)
@click.option(
    "-csv",
    required=True,
    multiple=True,
    help=(
        "CSV file to import. Multiple -csv parameters are allowed. "
        "See rstuf CLI guide for more details."
    ),
)
@click.option(
    "--skip-publish-targets",
    is_flag=True,
    help="Skip publishing targets in TUF Metadata.",
)
@click.pass_context
def import_targets(context, metadata_url, db_uri, csv, skip_publish_targets):
    """
    Import targets to RSTUF from exported CSV file.
    """
    settings = context.obj["settings"]
    server = settings.get("SERVER")
    token = settings.get("TOKEN")
    if server and token:
        token_access_check = is_logged(server, token)
        if token_access_check.state is False:
            raise click.ClickException(
                f"{str(token_access_check.data)}"
                "\n\nTry re-login: 'Repository Service for TUF admin login'"
            )
    else:
        raise click.ClickException("Login first. Run 'rstuf admin login'")

    headers = get_headers(settings)

    response = request_server(metadata_url, "1.bin.json", Methods.get)
    if response.status_code == 404:
        raise click.ClickException("RSTUF Metadata Targets not found.")

    # load all required infrastructure
    json_data = json.loads(response.text)
    targets = Metadata.from_dict(json_data)
    succinct_roles = targets.signed.delegations.succinct_roles
    engine = create_engine(f"{db_uri}")
    db_metadata = MetaData()
    db_client = engine.connect()
    rstuf_table = Table("rstuf_targets", db_metadata, autoload_with=engine)

    # validate if the CSV files are accessible
    _check_csv_files(csv)
    # import all CSV file(s) data to RSTUF DB without commiting
    _import_csv_to_rstuf(db_client, rstuf_table, csv, succinct_roles)

    # commit data into RSTUF DB
    console.print("Import status: Commiting all data to the RSTUF database")
    db_client.commit()
    console.print("Import status: All data imported to RSTUF DB")

    if skip_publish_targets:
        console.print(
            "Import status: Finshed. "
            "Not targets published (`--skip-publish-targets`)"
        )
    else:
        console.print("Import status: Submitting action publish targets")
        publish_targets = request_server(
            server, URL.publish_targets.value, Methods.post, headers=headers
        )
        if publish_targets.status_code != 202:
            raise click.ClickException(
                f"Failed to publish targets. {publish_targets.text}"
            )
        task_id = publish_targets.json()["data"]["task_id"]
        console.print(f"Import status: Publish targets task id is {task_id}")

        # monitor task status
        result = task_status(task_id, server, headers, "Import status: task ")
        if result is not None:
            console.print("Import status: [green]Finished.[/]")
