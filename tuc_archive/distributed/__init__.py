# tuc-archive — Archive a login-protected TYPO3 tx_tucforum forum into a Kiwix ZIM.
# Copyright (C) 2026 Konstantinos Pisimisis (CodeMaestro1)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Optional distributed crawling: a single coordinator + many workers.

Model (master/slave, REST over HTTP):
  - coordinator owns the authoritative CrawlState (queue/visited/errors) and a
    shared output dir; persists state atomically like the standalone crawler.
  - workers authenticate with a shared secret, claim URL batches, crawl, write
    content into the shared store, and report completions + newly-discovered
    links back. The coordinator dedups globally and reassigns jobs whose worker
    went silent (lease timeout).
"""
