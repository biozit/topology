from collections import defaultdict
from typing import Dict, List, Optional

from webapp.common import is_null, readfile, PreJSON, XROOTD_CACHE_SERVER, XROOTD_ORIGIN_SERVER
from webapp.exceptions import DataError, ResourceNotRegistered, ResourceMissingService
from webapp.ldap_data import get_ligo_ldap_dn_list
from webapp.models import GlobalData
from webapp.topology import Resource, ResourceGroup, Topology
from webapp.vos_data import AuthMethod, DNAuth, SciTokenAuth, Namespace, \
    parse_authz, ANY, ANY_PUBLIC


import logging

log = logging.getLogger(__name__)


def _log_or_raise(suppress_errors: bool, an_exception: BaseException, logmethod=log.debug):
    if suppress_errors:
        logmethod("%s %s", type(an_exception), an_exception)
    else:
        raise an_exception


def _resource_has_cache(resource: Resource) -> bool:
    return XROOTD_CACHE_SERVER in resource.service_names


def _get_resource_with_service(fqdn: Optional[str], service_name: str, topology: Topology,
                               suppress_errors: bool) -> Optional[Resource]:
    """If given an FQDN, returns the Resource _if it has the given service.
    If given None, returns None.
    If multiple Resources have the same FQDN, checks the first one.
    If suppress_errors is False, raises an exception on the following conditions:
    - no Resource matching FQDN (ResourceNotRegistered)
    - Resource does not provide a SERVICE_NAME (ResourceMissingService)
    If suppress_errors is True, logs the error and returns None on the above conditions.

    """
    resource = None
    if fqdn:
        resource = topology.safe_get_resource_by_fqdn(fqdn)
        if not resource:
            _log_or_raise(suppress_errors, ResourceNotRegistered(fqdn=fqdn))
            return None
        if service_name not in resource.service_names:
            _log_or_raise(
                suppress_errors,
                ResourceMissingService(resource, service_name)
            )
            return None
    return resource


def _get_cache_resource(fqdn: Optional[str], topology: Topology, suppress_errors: bool) -> Optional[Resource]:
    """Convenience wrapper around _get_resource-with-service() for a cache"""
    return _get_resource_with_service(fqdn, XROOTD_CACHE_SERVER, topology, suppress_errors)


def _get_origin_resource(fqdn: Optional[str], topology: Topology, suppress_errors: bool) -> Optional[Resource]:
    """Convenience wrapper around _get_resource-with-service() for an origin"""
    return _get_resource_with_service(fqdn, XROOTD_ORIGIN_SERVER, topology, suppress_errors)


def resource_allows_namespace(resource: Resource, namespace: Optional[Namespace]) -> bool:
    """Return True if the given resource's (cache or origin) AllowedVOs allows a namespace, which happens if:

    - The namespace's VO is in the AllowedVOs list, or
    - The namespace is public and ANY_PUBLIC is in the AllowedVOs list, or
    - ANY is in the AllowedVOs list; in this case, namespace may be `None`

    This says nothing about whether the namespace allows the cache/origin.
    namespace may be None, in which case thie returns true only if ANY is in the AllowedVOs list.
    No type/service checking is done in this function.
    """
    allowed_vos = resource.data.get("AllowedVOs", [])
    if ANY in allowed_vos:
        return True
    if namespace:
        if ANY_PUBLIC in allowed_vos and namespace.is_public():
            return True
        elif namespace.vo_name in allowed_vos:
            return True
    return False


def namespace_allows_origin_resource(namespace: Namespace, origin: Optional[Resource]) -> bool:
    """Return True if the given namespace allows a given origin resouce, which happens if
    the origin resource's name is in the namespace's AllowedOrigins list.
    Return False if origin is None.

    This says nothing about whether the origin allows the namespace.
    No type/service checking is done in this function.
    """
    return origin and origin.name in namespace.allowed_origins


def namespace_allows_cache_resource(namespace: Namespace, cache: Optional[Resource]) -> bool:
    """Return True if the given namespace allows a given cache resource, which happens if:

    - The cache resource's name is in the namespace's AllowedCaches list, or
    - The namespace's AllowedCaches list contains ANY; in this cache, cache may be None.

    This says nothing about whether the cache allows the namespace.
    No type/service checking is done in this function.
    """
    if ANY in namespace.allowed_caches:
        return True
    return cache and cache.name in namespace.allowed_caches


def get_supported_caches_for_namespace(namespace: Namespace, topology: Topology) -> List[Resource]:
    """Return a list of Resource objects of all caches that support a namespace.  This means the cache allows
    the namespace, AND the namespace allows the cache.
    """
    resource_groups = topology.get_resource_group_list()
    all_caches = [resource
                  for group in resource_groups
                  for resource in group.resources
                  if _resource_has_cache(resource)]
    return [cache
            for cache in all_caches
            if namespace_allows_cache_resource(namespace, cache)
            and resource_allows_namespace(cache, namespace)]


def generate_cache_authfile(global_data: GlobalData,
                            fqdn=None,
                            legacy=True,
                            suppress_errors=True) -> str:
    """
    Generate the Xrootd authfile needed by an StashCache cache server.  This contains authenticated data only,
    no public directories.
    """
    authfile = ""
    warnings = ""
    id_to_dir = defaultdict(set)
    id_to_str = {}

    topology = global_data.get_topology()
    resource = None
    if fqdn:
        resource = _get_cache_resource(fqdn, topology, suppress_errors)
        if not resource:
            return ""

    ligo_authz_list: List[AuthMethod] = []

    def fetch_ligo_authz_list_if_needed():
        if not ligo_authz_list:
            for dn in global_data.get_ligo_dn_list():
                ligo_authz_list.append(parse_authz(f"DN:{dn}")[0])
        return ligo_authz_list

    vos_data = global_data.get_vos_data()
    for stashcache_obj in vos_data.stashcache_by_vo_name.values():
        for dirname, namespace in stashcache_obj.namespaces.items():
            if not namespace_allows_cache_resource(namespace, resource):
                continue
            if resource and not resource_allows_namespace(resource, namespace):
                continue
            if namespace.is_public():
                continue

            # Extend authz list with LIGO DNs if applicable
            extended_authz_list = namespace.authz_list
            if dirname == "/user/ligo":
                if legacy:
                    extended_authz_list += fetch_ligo_authz_list_if_needed()
                else:
                    warnings += "# LIGO DNs unavailable\n"

            for authz in extended_authz_list:
                if authz.used_in_authfile:
                    id_to_dir[authz.get_authfile_id()].add(dirname)
                    id_to_str[authz.get_authfile_id()] = str(authz)

    # TODO: improve message and turn this into a warning
    if not id_to_dir:
        raise DataError("Cache does not support any protected namespaces")

    for authfile_id in id_to_dir:
        paths_acl = " ".join(f"{p} rl" for p in sorted(id_to_dir[authfile_id]))
        authfile += f"# {id_to_str[authfile_id]}\n"
        authfile += f"{authfile_id} {paths_acl}\n"

    return warnings + authfile


def generate_public_cache_authfile(global_data: GlobalData, fqdn=None, legacy=True, suppress_errors=True) -> str:
    """
    Generate the Xrootd authfile needed for public caches.  This contains public data only, no authenticated data.
    """
    _ = legacy
    authfile = "u * /user/ligo -rl \\\n"

    topology = global_data.get_topology()
    resource = None
    if fqdn:
        resource = _get_cache_resource(fqdn, topology, suppress_errors)
        if not resource:
            return ""

    public_dirs = set()
    vos_data = global_data.get_vos_data()
    for stashcache_obj in vos_data.stashcache_by_vo_name.values():
        for dirname, namespace in stashcache_obj.namespaces.items():
            if not namespace_allows_cache_resource(namespace, resource):
                continue
            if resource and not resource_allows_namespace(resource, namespace):
                continue
            if namespace.is_public():
                public_dirs.add(dirname)

    # TODO: improve message and turn this into a warning
    if not public_dirs:
        raise DataError("Cache does not support any public namespaces")

    for dirname in sorted(public_dirs):
        authfile += "    {} rl \\\n".format(dirname)

    # Delete trailing ' \' from the last line
    if authfile.endswith(" \\\n"):
        authfile = authfile[:-3] + "\n"

    return authfile


def generate_cache_scitokens(global_data: GlobalData, fqdn: str, suppress_errors=True) -> str:
    """
    Generate the SciTokens needed by a StashCache cache server, given the fqdn
    of the cache server.

    Returns a file with a dummy "issuer" block if there are no `- SciTokens:` blocks.

    If suppress_errors is True, returns an empty string on various error conditions (e.g. no fqdn,
    no resource matching fqdn, resource does not contain an origin server, etc.).  Otherwise, raises
    ValueError or DataError.
    """

    topology = global_data.get_topology()
    vos_data = global_data.get_vos_data()

    cache_resource = _get_cache_resource(fqdn, topology, suppress_errors)
    if not cache_resource:
        return ""

    template = """\
[Global]
audience = {allowed_vos_str}

{issuer_blocks_str}
"""

    allowed_vos = set()
    cache_authz_list = []

    for vo_name, stashcache_obj in vos_data.stashcache_by_vo_name.items():
        for namespace in stashcache_obj.namespaces.values():  # type: Namespace
            if namespace.is_public():
                continue
            if not namespace_allows_cache_resource(namespace, cache_resource):
                continue
            if not resource_allows_namespace(cache_resource, namespace):
                continue

            for authz in namespace.authz_list:
                if authz.used_in_scitokens_conf:
                    cache_authz_list.append(authz)
                    allowed_vos.add(vo_name)

    # Older plugin versions require at least one issuer block (SOFTWARE-4389)
    if not cache_authz_list:
        dummy_auth = SciTokenAuth(issuer="https://scitokens.org/nonexistent", base_path="/no-issuers-found",
                                  restricted_path=None, map_subject=False)
        cache_authz_list.append(dummy_auth)

    issuer_blocks = set(a.get_scitokens_conf_block(XROOTD_CACHE_SERVER) for a in cache_authz_list)
    issuer_blocks_str = "\n".join(sorted(issuer_blocks))
    allowed_vos_str = ", ".join(sorted(allowed_vos))

    return template.format(**locals()).rstrip() + "\n"


def generate_origin_authfile(global_data: GlobalData, fqdn: str, suppress_errors=True, public_origin=False) -> str:
    """
    Generate the XRootD Authfile needed by a StashCache origin server, given the FQDN
    of the origin server and whether it's the public or authenticated origin instance you're generating for.

    If suppress_errors is True, returns an empty string on various error conditions (e.g. no fqdn,
    no resource matching fqdn, resource does not contain an origin server, etc.).  Otherwise, raises
    ValueError or DataError.
    """
    topology = global_data.get_topology()
    vos_data = global_data.get_vos_data()
    origin_resource = None
    if fqdn:
        origin_resource = _get_origin_resource(fqdn, topology, suppress_errors)
        if not origin_resource:
            return ""

    public_paths = set()
    id_to_paths = defaultdict(set)
    id_to_str = {}
    warnings = []

    for vo_name, stashcache_obj in vos_data.stashcache_by_vo_name.items():
        for path, namespace in stashcache_obj.namespaces.items():
            if not namespace_allows_origin_resource(namespace, origin_resource):
                continue
            if not resource_allows_namespace(origin_resource, namespace):
                continue
            if namespace.is_public():
                public_paths.add(path)
                continue
            if public_origin:
                continue

            # The Authfile for origins should contain only caches and the origin itself, via SSL (i.e. DNs).
            # Ignore FQANs and DNs listed in the namespace's authz list.
            authz_list = []

            allowed_resources = [origin_resource]
            # Add caches
            allowed_caches = get_supported_caches_for_namespace(namespace, topology)
            if allowed_caches:
                allowed_resources.extend(allowed_caches)
            else:
                # TODO This situation should be caught by the CI
                warnings.append(f"# WARNING: No working cache / namespace combinations found for {path}")

            for resource in allowed_resources:
                dn = resource.data.get("DN")
                if dn:
                    authz_list.append(DNAuth(dn))
                else:
                    warnings.append(
                        f"# WARNING: Resource {resource.name} was skipped for VO {vo_name}, namespace {path}"
                        f" because the resource does not provide a DN."
                    )
                    continue

            for authz in authz_list:
                if authz.used_in_authfile:
                    id_to_paths[authz.get_authfile_id()].add(path)
                    id_to_str[authz.get_authfile_id()] = str(authz)

    if not id_to_paths and not public_paths:
        raise DataError("Origin does not support any namespaces")

    authfile_lines = []
    authfile_lines.extend(warnings)
    for authfile_id in id_to_paths:
        paths_acl = " ".join(f"{p} lr" for p in sorted(id_to_paths[authfile_id]))
        authfile_lines.append(f"# {id_to_str[authfile_id]}")
        authfile_lines.append(f"{authfile_id} {paths_acl}")

    # Public paths must be at the end
    if public_origin and public_paths:
        authfile_lines.append("")
        paths_acl = " ".join(f"{p} lr" for p in sorted(public_paths))
        authfile_lines.append(f"u * {paths_acl}")

    return "\n".join(authfile_lines) + "\n"


def generate_origin_scitokens(global_data: GlobalData, fqdn: str, suppress_errors=True) -> str:
    """
    Generate the SciTokens needed by a StashCache origin server, given the fqdn
    of the origin server.

    Returns a file with a dummy "issuer" block if there are no `- SciTokens:` blocks.

    If suppress_errors is True, returns an empty string on various error conditions (e.g. no fqdn,
    no resource matching fqdn, resource does not contain an origin server, etc.).  Otherwise, raises
    ValueError or DataError.
    """

    topology = global_data.get_topology()
    vos_data = global_data.get_vos_data()

    origin_resource = _get_origin_resource(fqdn, topology, suppress_errors)
    if not origin_resource:
        return ""

    template = """\
[Global]
audience = {allowed_vos_str}

{issuer_blocks_str}
"""

    allowed_vos = set()
    origin_authz_list = []

    for vo_name, stashcache_obj in vos_data.stashcache_by_vo_name.items():
        for namespace in stashcache_obj.namespaces.values():
            if namespace.is_public():
                continue
            if not namespace_allows_origin_resource(namespace, origin_resource):
                continue
            if not resource_allows_namespace(origin_resource, namespace):
                continue

            for authz in namespace.authz_list:
                if authz.used_in_scitokens_conf:
                    origin_authz_list.append(authz)
                    allowed_vos.add(vo_name)

    # Older plugin versions require at least one issuer block (SOFTWARE-4389)
    if not origin_authz_list:
        dummy_auth = SciTokenAuth(issuer="https://scitokens.org/nonexistent", base_path="/no-issuers-found",
                                  restricted_path=None, map_subject=False)
        origin_authz_list.append(dummy_auth)

    issuer_blocks = set(a.get_scitokens_conf_block(XROOTD_ORIGIN_SERVER) for a in origin_authz_list)
    issuer_blocks_str = "\n".join(sorted(issuer_blocks))
    allowed_vos_str = ", ".join(sorted(allowed_vos))

    return template.format(**locals()).rstrip() + "\n"


def get_namespaces_info(global_data: GlobalData) -> PreJSON:
    """Return data for the /stashcache/namespaces JSON endpoint.

    This includes a list of caches, with some data about cache endpoints,
    and a list of namespaces with some data about each namespace; see README.md for details.

    """
    # Helper functions
    def _cache_resource_dict(r: Resource):
        endpoint = f"{r.fqdn}:8000"
        auth_endpoint = f"{r.fqdn}:8443"
        for svc in r.services:
            if svc.get("Name") == XROOTD_CACHE_SERVER:
                if not is_null(svc, "Details", "endpoint_override"):
                    endpoint = svc["Details"]["endpoint_override"]
                if not is_null(svc, "Details", "auth_endpoint_override"):
                    auth_endpoint = svc["Details"]["auth_endpoint_override"]
                break
        return {"endpoint": endpoint, "auth_endpoint": auth_endpoint, "resource": r.name}

    def _namespace_dict(ns: Namespace):
        nsdict = {
            "path": ns.path,
            "readhttps": not ns.is_public(),
            "usetokenonread": any(isinstance(a, SciTokenAuth) for a in ns.authz_list),
            "writebackhost": ns.writeback,
            "dirlisthost": ns.dirlist,
            "caches": [],
        }

        for cache_name, cache_resource_obj in cache_resource_objs.items():
            if resource_allows_namespace(cache_resource_obj, ns) and namespace_allows_cache_resource(ns, cache_resource_obj):
                nsdict["caches"].append(cache_resource_dicts[cache_name])
        return nsdict
    # End helper functions

    resource_groups: List[ResourceGroup] = global_data.get_topology().get_resource_group_list()
    vos_data = global_data.get_vos_data()

    cache_resource_objs = {}  # type: Dict[str, Resource]
    cache_resource_dicts = {}  # type: Dict[str, Dict]

    for group in resource_groups:
        for resource in group.resources:
            if _resource_has_cache(resource):
                cache_resource_objs[resource.name] = resource
                cache_resource_dicts[resource.name] = _cache_resource_dict(resource)

    result_namespaces = []
    for stashcache_obj in vos_data.stashcache_by_vo_name.values():
        for namespace in stashcache_obj.namespaces.values():
            result_namespaces.append(_namespace_dict(namespace))

    return PreJSON({
        "caches": list(cache_resource_dicts.values()),
        "namespaces": result_namespaces
    })
