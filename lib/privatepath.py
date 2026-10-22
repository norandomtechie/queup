import os

def resolve_private_path(environ):
    """Return <account>/private/<tree>/ for the tree (queup, queup-dev, ...) serving this request."""
    script_path = os.path.abspath(environ['SCRIPT_FILENAME'])
    parts = script_path.split(os.sep)
    idx = parts.index('public_html')
    account_root = os.sep.join(parts[:idx])
    tree_name = parts[idx + 1]
    return account_root + '/private/' + tree_name + '/'

def resolve_tree_root(environ):
    """Absolute path of the public tree serving this request."""
    script_path = os.path.abspath(environ['SCRIPT_FILENAME'])
    parts = script_path.split(os.sep)
    idx = parts.index('public_html')
    return os.sep.join(parts[:idx + 2])
