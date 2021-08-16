import os
from pathlib import Path
from typing import Optional, Dict, Any

import click
from ruamel import yaml
from ruamel.yaml import Loader, SafeLoader

from asphalt.core.runner import run_application, policies
from asphalt.core.utils import merge_config, qualified_name


def env_constructor(loader, node):
    value = loader.construct_scalar(node)
    return os.getenv(value)


def text_file_constructor(loader, node):
    value = loader.construct_scalar(node)
    return Path(value).read_text()


def binary_file_constructor(loader, node):
    value = loader.construct_scalar(node)
    return Path(value).read_bytes()


for loader_cls in Loader, SafeLoader:
    loader_cls.add_constructor('!Env', env_constructor)
    loader_cls.add_constructor('!TextFile', text_file_constructor)
    loader_cls.add_constructor('!BinaryFile', binary_file_constructor)


@click.group()
def main():
    pass  # pragma: no cover


@main.command(help='Read one or more configuration files and start the application.')
@click.argument('configfile', type=click.File(), nargs=-1, required=True)
@click.option('--unsafe', is_flag=True, default=False,
              help='use unsafe mode when loading YAML (enables markup extensions)')
@click.option('-l', '--loop', type=click.Choice(policies.names),
              help='alternate event loop policy')
@click.option('-s', '--service', type=str,
              help='service to run (if the configuration file contains multiple services)')
def run(configfile, unsafe: bool, loop: Optional[str], service: Optional[str]):
    # Read the configuration from the supplied YAML files
    config = {}  # type: Dict[str, Any]
    for path in configfile:
        config_data = yaml.load(path, Loader=Loader if unsafe else SafeLoader)
        assert isinstance(config_data, dict), 'the document root element must be a dictionary'
        config = merge_config(config, config_data)

    # Override the event loop policy if specified
    if loop:
        config['event_loop_policy'] = loop

    services = config.pop('services', {})
    if not isinstance(services, dict):
        raise click.ClickException('The "services" key must be a dict, not {}'.format(
            qualified_name(services)))

    # If "component" was defined, use that as the default service if one has not been defined yet
    if 'component' in config:
        component = config.pop('component')
        services.setdefault('default', dict(component=component))

    # Try to figure out which service to launch
    service = service or os.getenv('ASPHALT_SERVICE')
    if len(services) == 0:
        raise click.ClickException('No services have been defined')
    elif service:
        try:
            service_config = services[service]
        except KeyError:
            raise click.ClickException(
                'Service {!r} has not been defined'.format(service)) from None
    elif len(services) == 1:
        service_config = next(iter(services.values()))
    elif 'default' in services:
        service_config = services['default']
    else:
        raise click.ClickException(
            'Multiple services present in configuration file but no default service has been '
            'defined and no service was explicitly selected with -s / --service')

    # Merge the service-level configuration with the top level one
    config = merge_config(config, service_config)

    # Start the application
    run_application(**config)
