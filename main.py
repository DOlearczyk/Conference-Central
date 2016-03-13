#!/usr/bin/env python

"""
main.py -- Udacity conference server-side Python App Engine
    HTTP controller handlers for memcache & task queue access

"""

import webapp2
from google.appengine.api import app_identity
from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.ext import ndb
from conference import ConferenceApi
from models import Session
from conference import MEMCACHE_FEATURED_SPEAKER


class SetAnnouncementHandler(webapp2.RequestHandler):
    def get(self):
        """Set Announcement in Memcache."""
        ConferenceApi._cacheAnnouncement()
        self.response.set_status(204)


class SendConfirmationEmailHandler(webapp2.RequestHandler):
    def post(self):
        """Send email confirming Conference creation."""
        mail.send_mail(
            'noreply@%s.appspotmail.com' % (
                app_identity.get_application_id()),  # from
            self.request.get('email'),  # to
            'You created a new Conference!',  # subj
            'Hi, you have created a following '  # body
            'conference:\r\n\r\n%s' % self.request.get(
                'conferenceInfo')
        )


class SetFeaturedSpeakerHandler(webapp2.RequestHandler):
    def post(self):
        """Set FeaturedSpeaker in Memcache."""
        # Get sessions from speaker given on conference
        sessions = Session.query(
            ndb.AND(Session.speakerKey == self.request.get('speakerKey'),
                    Session.websafeConferenceKey == self.request.get(
                        'websafeConferenceKey')))
        if sessions.count() > 1:
            memcache.set(MEMCACHE_FEATURED_SPEAKER,
                         '%s is Featured Speaker with sessions ' % self.request.get(
                             'speakerName')+", ".join([session.name for session in sessions]))
        # Update Speaker with Session
        ConferenceApi._updateSpeaker(self.request)

app = webapp2.WSGIApplication([
    ('/crons/set_announcement', SetAnnouncementHandler),
    ('/tasks/send_confirmation_email', SendConfirmationEmailHandler),
    ('/tasks/set_featured_speaker', SetFeaturedSpeakerHandler)
], debug=True)
