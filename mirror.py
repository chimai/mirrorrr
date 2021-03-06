#!/usr/bin/env python
# Copyright 2008 Brett Slatkin
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__author__ = ["Brett Slatkin (bslatkin@gmail.com)", "liruqi@gmail.com"]

import datetime
import hashlib
import logging
import pickle
import re
import time
import urllib
import string
import wsgiref.handlers

from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.ext import db
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.runtime import apiproxy_errors

import transform_content

################################################################################

DEBUG = False
EXPIRATION_DELTA_SECONDS = 3600
EXPIRATION_RECENT_URLS_SECONDS = 90

## DEBUG = True
## EXPIRATION_DELTA_SECONDS = 10
## EXPIRATION_RECENT_URLS_SECONDS = 1

HTTP_PREFIX = "http://"
HTTPS_PREFIX = "https://"
HSTS_DOMAINS = {
    "greatfire.org": 1,
    "github.com": 1,
    "www.github.com": 1,
}

BAD_HOSTS = ["pixel.quantserve.com", "hzs14.cnzz.com", "armdl.adobe.com", "192.168.1.233", "wpi.renren.com", "b.scorecardresearch.com", "www.google-analytics.com"]
IGNORE_HEADERS = frozenset([
  'set-cookie',
  'expires',
  'cache-control',

  # Ignore hop-by-hop headers
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailers',
  'transfer-encoding',
  'upgrade',
])

TRANSFORMED_CONTENT_TYPES = frozenset([
  "text/html",
  "text/css",
])

MIRROR_HOSTS = frozenset([
  'mirrorr.com',
  'mirrorrr.com',
  'www.mirrorr.com',
  'www.mirrorrr.com',
  'www1.mirrorrr.com',
  'www2.mirrorrr.com',
  'www3.mirrorrr.com',
])

MAX_CONTENT_SIZE = 10 ** 6

MAX_URL_DISPLAY_LENGTH = 50

################################################################################

def get_url_key_name(url):
  url_hash = hashlib.sha256()
  url_hash.update(url)
  return "hash_" + url_hash.hexdigest()

################################################################################

class EntryPoint(db.Model):
  translated_address = db.TextProperty(required=True)
  last_updated = db.DateTimeProperty(auto_now=True)
  display_address = db.TextProperty()


class MirroredContent(object):
  def __init__(self, original_address, translated_address,
               status, headers, data, base_url):
    self.original_address = original_address
    self.translated_address = translated_address
    self.status = status
    self.headers = headers
    self.data = data
    self.base_url = base_url

  @staticmethod
  def get_by_key_name(key_name):
    return memcache.get(key_name)

  @staticmethod
  def fetch_and_store(key_name, base_url, translated_address, mirrored_url, method, body):
    """Fetch and cache a page.
    
    Args:
      key_name: Hash to use to store the cached page.
      base_url: The hostname of the page that's being mirrored.
      translated_address: The URL of the mirrored page on this site.
      mirrored_url: The URL of the original page. Hostname should match
        the base_url.
    
    Returns:
      A new MirroredContent object, if the page was successfully retrieved.
      None if any errors occurred or the content could not be retrieved.
    """
    # Check for the X-Mirrorrr header to ignore potential loops.
    if base_url in MIRROR_HOSTS:
      logging.warning('Encountered recursive request for "%s"; ignoring',
                      mirrored_url)
      return None

    logging.debug("Fetching '%s'", mirrored_url)
    try:
      response = urlfetch.fetch(mirrored_url, 
                                follow_redirects=False, 
                                method=method,
                                payload=body)
    except (urlfetch.Error, apiproxy_errors.Error):
      logging.exception("Could not fetch URL")
      return None

    adjusted_headers = {}
    for key, value in response.headers.iteritems():
      adjusted_key = key.lower()
      if adjusted_key not in IGNORE_HEADERS:
        adjusted_headers[adjusted_key] = value

    content = response.content
    page_content_type = adjusted_headers.get("content-type", "")
    for content_type in TRANSFORMED_CONTENT_TYPES:
      # Startswith() because there could be a 'charset=UTF-8' in the header.
      if page_content_type.startswith(content_type):
        content = transform_content.TransformContent(base_url, mirrored_url,
                                                     content)
        break

    # If the transformed content is over 1MB, truncate it (yikes!)
    if len(content) > MAX_CONTENT_SIZE:
      logging.warning('Content is over 1MB; ignoring')
      #content = content[:MAX_CONTENT_SIZE]
      return None

    new_content = MirroredContent(
      base_url=base_url,
      original_address=mirrored_url,
      translated_address=translated_address,
      status=response.status_code,
      headers=adjusted_headers,
      data=content)
    if not memcache.add(key_name, new_content, time=EXPIRATION_DELTA_SECONDS):
      logging.error('memcache.add failed: key_name = "%s", '
                    'original_url = "%s"', key_name, mirrored_url)
      
    return new_content

################################################################################

class BaseHandler(webapp.RequestHandler):
  def get_relative_url(self):
    slash = self.request.url.find("/", len(self.request.scheme + "://"))
    if slash == -1:
      return "/"
    return self.request.url[slash:]


class HomeHandler(BaseHandler):
  def get(self):
    # Handle the input form to redirect the user to a relative url
    form_url = self.request.get("url")
    if form_url:
      # Accept URLs that still have a leading 'http://'
      inputted_url = urllib.unquote(form_url)
      if inputted_url.startswith(HTTP_PREFIX):
        inputted_url = inputted_url[len(HTTP_PREFIX):]
      return self.redirect("/" + inputted_url)

    # how we store data.
    secure_url = None
    if self.request.scheme == "http":
      secure_url = "https://mirrorrr.appspot.com"
    context = {
      "secure_url": secure_url,
    }
    self.response.out.write(template.render("main.html", context))

class HowHandler(BaseHandler):
  def get(self):
    self.response.out.write(template.render("how.html", {}))

class MirrorHandler(BaseHandler):
  def printable(self, s):
    for c in s:
      if c in string.printable:
        return False
    return True
  def get(self, base_url):
    return self.mirror(base_url, urlfetch.GET)
  def post(self, base_url):
    return self.mirror(base_url, urlfetch.POST)

  def mirror(self, base_url, method):
    assert base_url

    if base_url in BAD_HOSTS:
      logging.warning('Encountered bad request "%s"; ignoring', base_url)
      return self.error(404)
    if re.match(r'[a-zA-Z\d-]{,63}(\.[a-zA-Z\d-]{,63})+', base_url) == None:
      logging.warning('Encountered bad domain "%s"; ignoring', base_url)
      return self.error(404)
    # Log the user-agent and referrer, to see who is linking to us.
    wcproxy = ""
    if "X-WCProxy" in self.request.headers: wcproxy = self.request.headers["X-WCProxy"]
    
    passwds = memcache.get("passwds")
    if passwds != None:
      if wcproxy == "":
        return self.redirect("http://wcproxy.sinaapp.com/#update")

      if self.request.headers["X-WCPasswd"] not in passwds:
        logging.debug('Password = "%s", not in "%s"',
                  self.request.headers["X-WCPasswd"],
                  str(passwds))
        return self.redirect("http://wcproxy.sinaapp.com/passwd.html")

    logging.debug('X-WCProxy = "%s", Base_url = "%s", url = "%s", clientIp = "%s"', wcproxy, base_url, self.request.url, self.request.remote_addr)

    translated_address = self.get_relative_url()[1:]  # remove leading /
    unquoted = urllib.unquote(translated_address)
    if self.printable(unquoted): translated_address = unquoted
    if translated_address == "favicon.ico":
      return self.redirect("/favicon.ico")
    mirrored_url = HTTP_PREFIX + translated_address

    if base_url in HSTS_DOMAINS:
      mirrored_url = HTTPS_PREFIX + translated_address
    if base_url in ["facebook.com", "www.facebook.com"]:
      return self.redirect(HTTPS_PREFIX + translated_address)

    # Use sha256 hash instead of mirrored url for the key name, since key
    # names can only be 500 bytes in length; URLs may be up to 2KB.
    key_name = get_url_key_name(mirrored_url)
    logging.info("Handling request for '%s' = '%s'", mirrored_url, key_name)

    content = MirroredContent.get_by_key_name(key_name)
    cache_miss = False
    if content is None or method == urlfetch.POST:
      logging.debug("Cache miss")
      cache_miss = True
      content = MirroredContent.fetch_and_store(key_name, base_url,
                                                translated_address,
                                                mirrored_url,
                                                method,
                                                self.request.body)
    if content is None:
      return self.error(404)

    for key, value in content.headers.iteritems():
      self.response.headers[key] = value
    if not DEBUG:
      self.response.headers['cache-control'] = \
        'max-age=%d' % EXPIRATION_DELTA_SECONDS

    self.response.set_status(content.status)
    self.response.out.write(content.data)


class AdminHandler(webapp.RequestHandler):
  def get(self):
    html = """<html>
          <body>
            <form method="post">
              <p>Add Password: <input type="text" name="passwd" /></p>
              <p><input type="submit" /></p>
            </form>
	"""
    
    current = memcache.get("passwds")
    if current != None:
      for i in current:
        html += i+"<br />"

    latest_urls = EntryPoint.gql("ORDER BY last_updated DESC").fetch(7500)

    # Generate a display address that truncates the URL, adds an ellipsis.
    # This is never actually saved in the Datastore.
    host_cnt = {}
    for entry_point in latest_urls:
      if not entry_point.translated_address:
        continue
      host = entry_point.translated_address.split('/')[0]
      if host in host_cnt:
        host_cnt[host] += 1
      else:
        host_cnt[host] = 1
    
    host_top = {}
    for host in host_cnt:
      if host_cnt[host]>3: 
        html += host + (" %d" % host_cnt[host]) + "<br />"
        host_top[host] = host_cnt[host]
      logging.info(host + (" %d" % host_cnt[host]))

    if not memcache.set('latest_host_cnt', host_top):
      logging.error('memcache.add failed: latest_urls')
    html+=      """</body>
        </html>
        """
    self.response.out.write(html)
  def post(self):
    passwd = self.request.get("passwd")
    current = memcache.get("passwds")
    if current is None:
      passwds = [passwd]
    else:
      passwds = current
      passwds.append(passwd)

    memcache.set("passwds", passwds)
    self.response.headers['content-type'] = 'text/plain'
    self.response.out.write("passwds: " + str(memcache.get("passwds")))

class KaboomHandler(webapp.RequestHandler):
  def get(self):
    self.response.headers['content-type'] = 'text/plain'
    self.response.out.write('Flush successful: %s' % memcache.flush_all())


class CleanupHandler(webapp.RequestHandler):
  """Cleans up EntryPoint records."""

  def get(self):
    keep_cleaning = True
    try:
      content_list = EntryPoint.gql('ORDER BY last_updated').fetch(25)
      keep_cleaning = (len(content_list) > 0)
      db.delete(content_list)
      
      if content_list:
        message = "Deleted %d entities" % len(content_list)
      else:
        keep_cleaning = False
        message = "Done"
    except (db.Error, apiproxy_errors.Error), e:
      keep_cleaning = True
      message = "%s: %s" % (e.__class__, e)

    context = {  
      'keep_cleaning': keep_cleaning,
      'message': message,
    }
    self.response.out.write(template.render('cleanup.html', context))

################################################################################

app = webapp.WSGIApplication([
  (r"/", HomeHandler),
  (r"/main", HomeHandler),
  (r"/how.html", HowHandler),
  (r"/kaboom", KaboomHandler),
  (r"/admin", AdminHandler),
  (r"/cleanup", CleanupHandler),
  (r"/([^/]+).*", MirrorHandler)
], debug=DEBUG)


def main():
  wsgiref.handlers.CGIHandler().run(app)


if __name__ == "__main__":
  main()
