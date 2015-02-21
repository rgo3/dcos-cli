import abc
import collections
import hashlib
import json
import os
import shutil
import subprocess

import git
import portalocker
import pystache
from dcos.api import errors, util

try:
    # Python 2
    from urlparse import urlparse
except ImportError:
    # Python 3
    from urllib.parse import urlparse

logger = util.get_logger(__name__)


PACKAGE_NAME_KEY = 'DCOS_PACKAGE_NAME'
PACKAGE_VERSION_KEY = 'DCOS_PACKAGE_VERSION'


def install(pkg, version, init_client, user_options, cfg):
    """Installs a package.

    :param pkg: The package to install
    :type pkg: Package
    :param version: The package version to install
    :type version: str
    :param init_client: The program to use to run the package
    :type init_client: object
    :param user_options: Package parameters
    :type user_options: dict
    :param cfg: Configuration dictionary
    :type cfg: dcos.api.config.Toml
    :rtype: Error
    """

    if user_options is None:
        user_options = {}

    config_schema, schema_error = pkg.config_json(version)

    if schema_error is not None:
        return schema_error

    default_options, default_error = _extract_default_values(config_schema)

    if default_error is not None:
        return default_error

    # Merge option overrides
    options = dict(list(default_options.items()) + list(user_options.items()))

    # Validate options with the config schema
    err = util.validate_json(options, config_schema)
    if err is not None:
        return err

    # Insert option parameters into the init template
    init_template, tmpl_error = pkg.marathon_template(version)

    if tmpl_error is not None:
        return tmpl_error

    init_desc = json.loads(pystache.render(init_template, options))

    # Add package metadata
    metadata, meta_error = pkg.package_json(version)

    if meta_error is not None:
        return meta_error

    init_desc['labels'] = {
        PACKAGE_NAME_KEY: metadata['name'],
        PACKAGE_VERSION_KEY: metadata['version']
    }

    # Validate the init descriptor
    # TODO(CD): Is this necessary / desirable at this point?

    # Send the descriptor to init
    _, err = init_client.add_app(init_desc)
    return err


def list_installed_packages(init_client):
    """
    :param init_client: The program to use to list packages
    :type init_client: object
    :rtype: ((str, str), Error)
    """

    apps, error = init_client.get_apps()
    if error is not None:
        return (None, error)

    pkgs = [(a['labels'][PACKAGE_NAME_KEY], a['labels'][PACKAGE_VERSION_KEY])
            for a in apps
            if (a.get('labels') is not None and
                a.get('labels').get(PACKAGE_NAME_KEY) is not None and
                a.get('labels').get(PACKAGE_VERSION_KEY) is not None)]

    return (pkgs, None)


def search(query, cfg):
    """Returns a list of index entry collections, one for each registry in
    the supplied config.

    :param query: The search term
    :type query: str
    :param cfg: Configuration dictionary
    :type cfg: dcos.api.config.Toml
    :rtype: (list of IndexEntries, Error)
    """

    threshold = 0.5  # Minimum rank required to appear in results
    results = []

    def clean_package_entry(entry):
        result = entry.copy()
        result.update({
            'versions': list(entry['versions'].keys())
        })
        return result

    for registry in registries(cfg):
        source_results = []
        index, error = registry.get_index()
        if error is not None:
            return (None, error)

        for pkg in index['packages']:
            rank = _search_rank(pkg, query)
            if rank >= threshold:
                source_results.append(clean_package_entry(pkg))

        entries = IndexEntries(registry.source, source_results)
        results.append(entries)

    return (results, None)


def _search_rank(pkg, query):
    """
    :param pkg: Index entry to rank for affinity with the search term
    :type pkg: object
    :param query: Search term
    :type query: str
    :rtype: float
    """

    q = query.lower()

    result = 0.0
    if q in pkg['name'].lower():
        result += 2.0

    for tag in pkg['tags']:
        if q in tag.lower():
            result += 1.0

    if q in pkg['description'].lower():
        result += 0.5

    return result


def _extract_default_values(config_schema):
    """
    :param config_schema: A json-schema describing configuration options.
    :type config_schema: (dict, Error)
    """

    properties = config_schema.get('properties')
    schema_type = config_schema.get('type')

    if schema_type != 'object' or properties is None:
        return ({}, None)

    defaults = [(p, properties[p]['default'])
                for p in properties
                if 'default' in properties[p]]

    return (dict(defaults), None)


def resolve_package(package_name, config):
    """Returns the first package with the supplied name found by looking at
    the configured sources in the order they are defined.

    :param package_name: The name of the package to resolve
    :type package_name: str
    :param config: Configuration dictionary
    :type config: dcos.api.config.Toml
    :returns: The named package, if found
    :rtype: Package or None
    """

    for registry in registries(config):
        package, error = registry.get_package(package_name)
        if package is not None:
            return package

    return None


def registries(config):
    """Returns configured cached package registries.

    :param config: Configuration dictionary
    :type config: dcos.api.config.Toml
    :returns: The list of registries, in resolution order
    :rtype: list of Registry
    """

    sources, errors = list_sources(config)
    return [Registry(source, source.local_cache(config)) for source in sources]


def list_sources(config):
    """List configured package sources.

    :param config: Configuration dictionary
    :type config: dcos.api.config.Toml
    :returns: The list of sources, in resolution order
    :rtype: (list of Source, list of Error)
    """

    source_uris = config.get('package.sources')

    if source_uris is None:
        config_error = Error('No configured value for [package.sources]')
        return (None, [config_error])

    results = [url_to_source(s) for s in config['package.sources']]
    sources = [source for (source, _) in results if source is not None]
    errors = [error for (_, error) in results if error is not None]
    return (sources, errors)


def url_to_source(url):
    """Creates a package source from the supplied URL.

    :param url: Location of the package source
    :type url: str
    :returns: A Source backed by the supplied URL
    :rtype: (Source, Error)
    """

    parse_result = urlparse(url)
    scheme = parse_result.scheme

    if scheme == 'file':
        return (FileSource(url), None)

    elif scheme == 'http' or scheme == 'https':
        return (HttpSource(url), None)

    elif scheme == 'git':
        return (GitSource(url), None)

    else:
        err = Error("Source URL uses unsupported protocol [{}]".format(url))
        return (None, err)


def acquire_file_lock(lock_file_path):
    """Acquires an exclusive lock on the supplied file.

    :param lock_file_path: Path to the lock file
    :type lock_file_path: str
    :returns: Lock file descriptor
    :rtype: (file_descriptor, Error)
    """

    lock_fd = open(lock_file_path, 'w')
    acquire_mode = portalocker.LOCK_EX | portalocker.LOCK_NB

    try:
        portalocker.lock(lock_fd, acquire_mode)
        return (lock_fd, None)
    except portalocker.LockException:
        lock_fd.close()
        return (None, Error("Unable to acquire the package cache lock"))


def update_sources(config):
    """Overwrites the local package cache with the latest source data.

    :param config: Configuration dictionary
    :type config: dcos.api.config.Toml
    :returns: Error, if any.
    :rtype: list of Error
    """

    errors = []

    # ensure the cache directory is properly configured
    cache_dir = config.get('package.cache')

    if cache_dir is None:
        config_error = Error("No configured value for [package.cache]")
        errors.append(config_error)
        return errors

    # ensure the cache directory exists
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    if not os.path.isdir(cache_dir):
        err = Error('Cache directory does not exist! [{}]'.format(cache_dir))
        errors.append(err)
        return errors

    # obtain an exclusive file lock on $CACHE/.lock
    lock_path = os.path.join(cache_dir, '.lock')
    lock_fd, lock_error = acquire_file_lock(lock_path)

    if lock_error is not None:
        errors.append(lock_error)
        return errors

    with lock_fd:

        # list sources
        sources, list_errors = list_sources(config)

        if len(list_errors) > 0:
            errors = errors + list_errors
            return errors

        for source in sources:

            logger.info("Updating source [%s]", source)

            # create a temporary staging directory
            with util.tempdir() as tmp_dir:

                stage_dir = os.path.join(tmp_dir, source.hash())

                # copy to the staging directory
                copy_err = source.copy_to_cache(stage_dir)
                if copy_err is not None:
                    errors.append(copy_err)
                    continue  # keep updating the other sources

                # validate content
                validation_errors = Registry(source, stage_dir).validate()
                if len(validation_errors) > 0:
                    errors += validation_errors
                    continue  # keep updating the other sources

                # remove the $CACHE/source.hash() directory
                target_dir = os.path.join(cache_dir, source.hash())
                try:
                    if os.path.exists(target_dir):
                        shutil.rmtree(target_dir, ignore_errors=False)
                except OSError:
                    err = Error(
                        'Could not remove directory [{}]'.format(target_dir))
                    errors.append(err)
                    continue  # keep updating the other sources

                # move the staging directory to $CACHE/source.hash()
                shutil.move(stage_dir, target_dir)

    return errors


class Source:
    """A source of DCOS packages."""

    @property
    @abc.abstractmethod
    def url(self):
        """
        :returns: Location of the package source
        :rtype: str
        """

        raise NotImplementedError

    def hash(self):
        """Returns a cryptographically secure hash derived from this source.

        :returns: a hexadecimal string
        :rtype: str
        """

        return hashlib.sha1(self.url.encode('utf-8')).hexdigest()

    def local_cache(self, config):
        """Returns the file system path to this source's local cache.

        :param config: Configuration dictionary
        :type config: dcos.api.config.Toml
        :returns: Path to this source's local cache on disk
        :rtype: str or None
        """

        cache_dir = config.get('package.cache')
        if cache_dir is None:
            return None

        return os.path.join(cache_dir, self.hash())

    def copy_to_cache(self, target_dir):
        """Copies the source content to the supplied local directory.

        :param target_dir: Path to the destination directory.
        :type target_dir: str
        :returns: The error, if one occurred
        :rtype: Error
        """

        raise NotImplementedError

    def __repr__(self):

        return self.url


class FileSource(Source):
    """A registry of DCOS packages.

    :param url: Location of the package source
    :type url: str
    """

    def __init__(self, url):
        self._url = url

    @property
    def url(self):
        """
        :returns: Location of the package source
        :rtype: str
        """

        return self._url

    def copy_to_cache(self, target_dir):
        """Copies the source content to the supplied local directory.

        :param target_dir: Path to the destination directory.
        :type target_dir: str
        :returns: The error, if one occurred
        :rtype: Error
        """

        # copy the source to the target_directory
        parse_result = urlparse(self._url)
        source_dir = parse_result.path
        try:
            shutil.copytree(source_dir, target_dir)
            return None
        except OSError:
            return Error('Could not copy [{}] to [{}]'.format(source_dir,
                                                              target_dir))


class HttpSource(Source):
    """A registry of DCOS packages.

    :param url: Location of the package source
    :type url: str
    """

    def __init__(self, url):
        self._url = url

    @property
    def url(self):
        """
        :returns: Location of the package source
        :rtype: str
        """

        return self._url

    def copy_to_cache(self, target_dir):
        """Copies the source content to the supplied local directory.

        :param target_dir: Path to the destination directory.
        :type target_dir: str
        :returns: The error, if one occurred
        :rtype: Error
        """

        raise NotImplementedError


class GitSource(Source):
    """A registry of DCOS packages.

    :param url: Location of the package source
    :type url: str
    """

    def __init__(self, url):
        self._url = url

    @property
    def url(self):
        """
        :returns: Location of the package source
        :rtype: str
        """

        return self._url

    def copy_to_cache(self, target_dir):
        """Copies the source content to the supplied local directory.

        :param target_dir: Path to the destination directory.
        :type target_dir: str
        :returns: The error, if one occurred
        :rtype: Error
        """

        try:
            # TODO(SS): add better url parsing

            # Ensure git is installed properly.
            git_program = util.which('git')
            if git_program is None:
                return Error("""Could not locate the git program.  Make sure \
it is installed and on the system search path.
PATH = {}""".format(os.environ['PATH']))

            # Clone git repo into the supplied target directory.
            git.Repo.clone_from(self._url,
                                to_path=target_dir,
                                progress=None,
                                branch='master')

            # Remove .git directory to save space.
            shutil.rmtree(os.path.join(target_dir, ".git"))
            return None

        except git.exc.GitCommandError:
            return Error("Unable to clone [{}] to [{}]".format(self.url,
                                                               target_dir))


class Error(errors.Error):
    """Class for describing errors during packaging operations.

    :param message: Error message
    :type message: str
    """

    def __init__(self, message):
        self._message = message

    def error(self):
        """Return error message

        :returns: The error message
        :rtype: str
        """

        return self._message


class Registry():
    """Represents a package registry on disk.

    :param base_path: Path to the registry
    :type base_path: str
    :param source: The associated package source
    :type source: Source
    """

    def __init__(self, source, base_path):
        self._base_path = base_path
        self._source = source

    def validate(self):
        """Validates a package registry.

        :returns: Validation errors
        :rtype: list of Error
        """

        # TODO(CD): implement these checks in pure Python?
        scripts_dir = os.path.join(self._base_path, 'scripts')
        validate_script = os.path.join(scripts_dir, '1-validate-packages.sh')
        errors = []
        result = subprocess.call(validate_script)
        if result is not 0:
            errors.append(
                Error('Source tree is not valid [{}]'.format(self._base_path)))

        return errors

    @property
    def source(self):
        """Returns the associated upstream package source for this registry.

        :rtype: str
        """

        return self._source

    def get_index(self):
        """Returns the index of packages in this registry.

        :rtype: (object, Error)
        """

        # The package index is found in $BASE/repo/meta/index.json
        index_path = os.path.join(
            self._base_path,
            'repo',
            'meta',
            'index.json')

        if not os.path.isfile(index_path):
            return (None, Error('Path [{}] is not a file'.format(index_path)))

        try:
            with open(index_path) as fd:
                return (json.load(fd), None)
        except IOError:
            return (None, Error('Unable to open file [{}]'.format(index_path)))
        except ValueError:
            return (None, Error('Unable to parse [{}]'.format(index_path)))

    def get_package(self, package_name):
        """Returns the named package, if it exists.

        :param package_name: The name of the package to fetch
        :type package_name: str
        :returns: The requested package
        :rtype: (Package, Error)
        """

        if len(package_name) is 0:
            (None, Error('Package name must not be empty.'))

        # Packages are found in $BASE/repo/package/<first_character>/<pkg_name>
        first_character = package_name[0].title()

        package_path = os.path.join(
            self._base_path,
            'repo',
            'packages',
            first_character,
            package_name)

        if not os.path.isdir(package_path):
            return (None, Error("Package [{}] not found".format(package_name)))

        try:
            return (Package(package_path), None)

        except:
            error = Error('Could not read package [{}]'.format(package_name))
            return (None, error)


class Package():
    """Interface to a package on disk.

    :param path: Path to the package description on disk
    :type path: str
    """

    def __init__(self, path):

        assert os.path.isdir(path)
        self.path = path

    def name(self):
        """Returns the package name.

        :returns: The name of this package
        :rtype: str
        """

        return os.path.basename(self.path)

    def command_json(self, version):
        """Returns the JSON content of the command.json file.

        :returns: Package command data
        :rtype: (dict, Error)
        """

        return self._json(os.path.join(version, 'command.json'))

    def config_json(self, version):
        """Returns the JSON content of the config.json file.

        :returns: Package config schema
        :rtype: (dict, Error)
        """

        return self._json(os.path.join(version, 'config.json'))

    def package_json(self, version):
        """Returns the JSON content of the package.json file.

        :returns: Package data
        :rtype: (dict, Error)
        """

        return self._json(os.path.join(version, 'package.json'))

    def marathon_template(self, version):
        """Returns the JSON content of the marathon.json file.

        :returns: Package marathon data
        :rtype: str or Error
        """

        return self._data(os.path.join(version, 'marathon.json'))

    def _json(self, path):
        """Returns the json content of the supplied file, relative to the
        base path.

        :param path: The relative path to the file to read
        :type path: str
        :rtype: (dict, Error)
        """

        data, error = self._data(path)

        if error is not None:
            return (None, error)

        try:
            result = json.loads(data)
        except ValueError:
            return (None, Error(''))

        return (result, None)

    def _data(self, path):
        """Returns the content of the supplied file, relative to the base path.

        :param path: The relative path to the file to read
        :type path: str
        :returns: File content of the supplied path
        :rtype: (str, Error)
        """

        full_path = os.path.join(self.path, path)
        if not os.path.isfile(full_path):
            return (None, Error('Path [{}] is not a file'.format(full_path)))

        try:
            with open(full_path) as fd:
                content = fd.read()
                return (content, None)
        except IOError:
            return (None, Error('Unable to open file [{}]'.format(full_path)))

    def package_versions(self):
        """Returns all of the available package versions, most recent first.

        Note that the result does not describe versions of the package, not
        the software described by the package.

        :returns: Available versions of this package
        :rtype: list of str
        """

        vs = [f for f in os.listdir(self.path) if not f.startswith('.')]
        vs.reverse()
        return vs

    def software_versions(self):
        """Returns a mapping from the package version to the version of the
        software described by the package.

        :returns: Map from package versions to versions of the softwre.
        :rtype: (dict, Error)
        """

        software_package_map = collections.OrderedDict()
        for v in self.package_versions():
            pkg_json, error = self.package_json(v)
            if error is not None:
                return (None, error)
            software_package_map[v] = pkg_json['version']
        return (software_package_map, None)

    def latest_version(self):
        """Returns the latest package version.

        :returns: The latest version of this package
        :rtype: (str, Error)
        """

        pkg_versions = self.package_versions()

        if len(pkg_versions) is 0:
            return (None, Error(
                'No versions found for package [{}]'.format(self.name())))

        pkg_versions.sort()
        return (pkg_versions[-1], None)

    def __repr__(self):

        v, error = self.latest_version()
        if error is not None:
            return error.error()

        pkg_json, error = self.package_json(v)

        if error is not None:
            return error.error()

        return json.dumps(pkg_json)


class IndexEntries():
    """A collection of package index entries from a single source.
    Each entry is a dict as described by the JSON schema for the package index:
    https://github.com/mesosphere/universe/blob/master/repo/meta/schema/index-schema.json

    :param source: The source of these index entries
    :type source: Source
    :param packages: The index entries
    :type packages: list of dict
    """

    def __init__(self, source, packages):
        self._source = source
        self._packages = packages

    @property
    def source(self):
        """Returns the source of these index entries.

        :rtype: Source
        """

        return self._source

    @property
    def packages(self):
        """Returns the package index entries.

        :rtype: list of dict
        """

        return self._packages

    def as_dict(self):
        """
        :rtype: dict
        """

        return {
          'source': self.source.url,
          'packages': self.packages
        }
