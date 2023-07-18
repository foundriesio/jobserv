import json

from flask import redirect

from jobserv.storage.gce_storage import Storage as GcpStorage, log


class Storage(GcpStorage):
    LINK_FILE = ".artifacts.lnk"

    def list_artifacts(self, run):
        artifacts = super().list_artifacts(run)
        if len(artifacts) == 1 and artifacts[0]["name"] == self.LINK_FILE:
            log.info("Gettings artifacts from link file")
            path = self._get_run_path(run, self.LINK_FILE)
            buf = self._get_as_string(path)
            links = json.loads(buf)["links"]
            return links
        return artifacts

    def get_download_response(self, request, run, path):
        base = self._get_run_path(run)
        b = self.bucket.blob(base + path)
        if not b.exists():
            try:
                buf = self._get_as_string(base + self.LINK_FILE)
                links = json.loads(buf)["links"]
                for link in links:
                    if link["name"] == path:
                        log.info("Redirecting for link file to %s", link["url"])
                        return redirect(link["url"])
            except FileNotFoundError:
                pass
        return super().get_download_response(request, run, path)

    def _generate_put_url(self, run, path, expiration, content_type):
        if path == self.LINK_FILE:
            # Linked storage should only be used when creating external builds
            raise ValueError(f"Invalid file name: {path}")
        return super()._generate_put_url(run, path, expiration, content_type)
