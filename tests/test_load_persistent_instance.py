# this tries to load a persistent instance created in a public s3 bucket
# with s3:GetObject and s3:ListBucket policies enabled for all
# the bucket is s3://lamin-site-assets

import lamindb_setup as ln_setup


def test_load_persistent_instance():
    ln_setup.load("testuser1/lamin-site-assets")
    ln_setup.close()
