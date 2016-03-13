#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

"""
from functools import wraps

from datetime import datetime

import endpoints
import json
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException, SpeakerForm, Speaker, SessionForm, \
    SpeakerForms, Session, SessionForms
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForms
from models import TeeShirtSize

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FEATURED_SPEAKER = "FEATURED_SPEAKER"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": ["Default", "Topic"],
}

OPERATORS = {
    'EQ': '=',
    'GT': '>',
    'GTEQ': '>=',
    'LT': '<',
    'LTEQ': '<=',
    'NE': '!='
}

FIELDS = {
    'CITY': 'city',
    'TOPIC': 'topics',
    'MONTH': 'month',
    'MAX_ATTENDEES': 'maxAttendees',
}

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SPEAKER_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSpeakerKey=messages.StringField(1),
)

SPEAKER_POST_REQUEST = endpoints.ResourceContainer(
    SpeakerForm,
    websafeSpeakerKey=messages.StringField(1),
)

SESSION_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)

SESSION_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeSessionKey=messages.StringField(1),
)

SESSION_GET_BY_CONF_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

SESSION_GET_BY_CONF_TYPE_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)

SESSION_GET_BY_TYPE_TIME_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    typeOfSession=messages.StringField(1),
    startTime=messages.StringField(2),
)


# - - - Utils - - - - - - - - - - - - - - - - - - - - - - - -

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        return f(*args, **kwargs)

    return decorated_function


# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
               allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID,
                                   ANDROID_CLIENT_ID, IOS_CLIENT_ID],
               scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

    # - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf

    @login_required
    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        if not request.name:
            raise endpoints.BadRequestException(
                "Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in
                request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10],
                                                  "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10],
                                                "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        user = endpoints.get_current_user()
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
                              'conferenceInfo': repr(request)},
                      url='/tasks/send_confirmation_email'
                      )
        return request

    @login_required
    @ndb.transactional()
    def _updateConferenceObject(self, request):
        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in
                request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        user = endpoints.get_current_user()
        user_id = getUserId(user)
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
                      http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
                      path='conference/{websafeConferenceKey}',
                      http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)

    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
                      path='conference/{websafeConferenceKey}',
                      http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
                      path='getConferencesCreated',
                      http_method='POST', name='getConferencesCreated')
    @login_required
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        user = endpoints.get_current_user()
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[
                self._copyConferenceToForm(conf, getattr(prof, 'displayName'))
                for conf in confs]
        )

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"],
                                                   filtr["operator"],
                                                   filtr["value"])
            q = q.filter(formatted_query)
        return q

    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in
                     f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException(
                    "Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException(
                        "Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)

    @endpoints.method(ConferenceQueryForms, ConferenceForms,
                      path='queryConferences',
                      http_method='POST',
                      name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in
                      conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[
                self._copyConferenceToForm(conf, names[conf.organizerUserId])
                for conf in \
                conferences]
        )

    # - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name,
                            getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf

    @login_required
    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key=p_key,
                displayName=user.nickname(),
                mainEmail=user.email(),
                teeShirtSize=str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile  # return Profile

    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        # if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        # else:
                        #    setattr(prof, field, val)
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)

    @endpoints.method(message_types.VoidMessage, ProfileForm,
                      path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()

    @endpoints.method(ProfileMiniForm, ProfileForm,
                      path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)

    # - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement

    @endpoints.method(message_types.VoidMessage, StringMessage,
                      path='conference/announcement/get',
                      http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(
            data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")

    # - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser()  # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
                      path='conferences/attending',
                      http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser()  # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in
                     prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in
                      conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[
            self._copyConferenceToForm(conf, names[conf.organizerUserId]) \
            for conf in conferences]
                               )

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
                      path='conference/{websafeConferenceKey}',
                      http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
                      path='conference/{websafeConferenceKey}',
                      http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
                      path='filterPlayground',
                      http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Conference.city == "London")
        q = q.filter(Conference.topics == "Medical Innovations")
        q = q.filter(Conference.month == 6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

    # Task 1:
    # - - - Speaker - - - - - - - - - - - - - - - - - - - -
    def _copySpeakerToForm(self, speaker):
        """Copy relevant fields from Speaker to SpeakerForm."""
        form = SpeakerForm()
        for field in form.all_fields():
            if hasattr(speaker, field.name):
                setattr(form, field.name, getattr(speaker, field.name))
            elif field.name == "websafeKey":
                setattr(form, field.name, speaker.key.urlsafe())
        form.check_initialized()
        return form

    @login_required
    def _createSpeakerObject(self, request):
        """Create Speaker object, returning SpeakerForm/request."""
        # Get request values into dictionary
        data = {field.name: getattr(request, field.name)
                for field in request.all_fields()}
        del data['websafeKey']
        # Create Speaker
        speaker = Speaker(**data).put()
        return request

    @login_required
    @ndb.transactional()
    def _updateSpeakerObject(self, request):
        """Update Speaker object, returning SpeakerForm/request."""
        # Get request values into dictionary
        data = {field.name: getattr(request, field.name)
                for field in request.all_fields()}

        # Get Speaker from speaker key
        speaker = ndb.Key(urlsafe=request.websafeSpeakerKey).get()
        # Check if he exists
        if not speaker:
            raise endpoints.NotFoundException(
                'No speaker found with key: %s' % request.websafeSpeakerKey)

        # Update values which were given in request
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # write to Speaker object
                setattr(speaker, field.name, data)
        speaker.put()
        return self._copySpeakerToForm(speaker)

    @endpoints.method(SpeakerForm, SpeakerForm, path='speaker/create',
                      http_method='POST', name='createSpeaker')
    def createSpeaker(self, request):
        """Create new Speaker."""
        return self._createSpeakerObject(request)

    @endpoints.method(SPEAKER_GET_REQUEST, SpeakerForm,
                      path='speaker/get/{websafeSpeakerKey}',
                      http_method='GET', name='getSpeaker')
    def getSpeaker(self, request):
        """Return requested speaker (by websafeSpeakerKey)."""
        # Get speaker
        speaker = ndb.Key(urlsafe=request.websafeSpeakerKey).get()
        # Check if exist
        if not speaker:
            raise endpoints.NotFoundException(
                'No speaker found with key: %s' % request.websafeSpeakerKey)
        return self._copySpeakerToForm(speaker)

    @endpoints.method(SPEAKER_POST_REQUEST, SpeakerForm,
                      path='speaker/edit/{websafeSpeakerKey}',
                      http_method='PUT', name='updateSpeaker')
    def updateSpeaker(self, request):
        """Update speaker with fields in request."""
        return self._updateSpeakerObject(request)

    @endpoints.method(message_types.VoidMessage, SpeakerForms,
                      path='speaker/get_all',
                      http_method='GET', name='GetAllSpeakers')
    def GetAllSpeakers(self, request):
        """Get all speakers."""
        # Query all speakers
        speakers = Speaker.query().fetch()
        # Return them invidually via Speaker Form
        return SpeakerForms(
            items=[self._copySpeakerToForm(item) for item in speakers]
        )

    # - - - Session - - - - - - - - - - - - - - - - - - - -
    def _copySessionToForm(self, session):
        """Copy fields from Session to SessionForm."""
        form = SessionForm()
        for field in form.all_fields():
            if hasattr(session, field.name):
                if field.name in ['startTime', 'date']:
                    setattr(
                        form, field.name, str(getattr(session, field.name)))
                else:
                    setattr(form, field.name, getattr(session, field.name))
            elif field.name == "websafeConferenceKey":
                setattr(form, field.name, session.key.urlsafe())
        form.check_initialized()
        return form

    @login_required
    def _createSessionObject(self, request):
        """Create Session object, returning SessionForm/request."""
        # Copy values into dictionary
        data = {field.name: getattr(request, field.name)
                for field in request.all_fields()}

        # Change data format
        if data['date']:
            data['date'] = datetime.strptime(
                data['date'][:10], "%Y-%m-%d").date()

        # Change time format
        if data['startTime']:
            data['startTime'] = datetime.strptime(
                data['startTime'][:5], "%H:%M").time()

        # Get Speaker
        speakerKey = ndb.Key(urlsafe=request.speakerKey)
        speaker = speakerKey.get()
        # Check if speaker exists
        if not speaker:
            raise endpoints.NotFoundException(
                'No speaker found with key: %s' % request.speakerKey)

        # Check if conference exists
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        conf = c_key.get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'
                % request.websafeConferenceKey)

        # Create session id
        s_id = Session.allocate_ids(size=1, parent=c_key)[0]
        s_key = ndb.Key(Session, s_id, parent=c_key)
        data['key'] = s_key
        # Create session entity
        session = Session(**data).put()
        # Task 4 - set memcache entry
        taskqueue.add(params={'speakerKey': request.speakerKey,
                              'speakerName': speaker.name,
                              'sessionName': request.name,
                              'websafeConferenceKey': request.websafeConferenceKey},
                      url='/tasks/set_featured_speaker')
        return request

    @login_required
    @ndb.transactional(xg=True)
    def _updateSessionObject(self, request):
        """Update Session object."""
        # Get data into dictionary
        data = {field.name: getattr(request, field.name)
                for field in request.all_fields()}
        del data['websafeSessionKey']
        del data['websafeConferenceKey']

        # Get session from session key
        session = ndb.Key(urlsafe=request.websafeSessionKey).get()
        # Check if session exists
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.websafeSessionKey)

        # Check if speaker key is valid
        if request.speakerKey not in (None, ''):
            speakerKey = ndb.Key(urlsafe=request.speakerKey)
            speaker = speakerKey.get()
            if not speaker:
                raise endpoints.NotFoundException(
                    'No speaker found with key: %s' % request.speakerKey)

        # Set changed fields
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('date'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                if field.name in ('startTime'):
                    data = datetime.strptime(data, "%H:%M").time()
                # write to Session object
                setattr(session, field.name, data)
        session.put()
        return self._copySessionToForm(session)

    @endpoints.method(SessionForm, SessionForm,
                      path='conference/session/create',
                      http_method='POST', name='createSession')
    def createSession(self, request):
        """Create Session Object"""
        return self._createSessionObject(request)

    @endpoints.method(SESSION_POST_REQUEST, SessionForm,
                      path='conference/session/update/{websafeSessionKey}',
                      http_method='PUT', name='updateSession')
    def updateSession(self, request):
        """Update Session Object"""
        return self._updateSessionObject(request)

    @endpoints.method(SESSION_GET_BY_CONF_REQUEST, SessionForms,
                      path='conference/session/get_by_conf/'
                           + '{websafeConferenceKey}',
                      http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Get Sessions by conference key"""
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        conf = c_key.get()
        # Check if conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'
                % request.websafeConferenceKey)
        # Get conference sessions
        sessions = Session.query(ancestor=c_key).fetch()
        return SessionForms(
            items=[self._copySessionToForm(item) for item in sessions]
        )

    @endpoints.method(SESSION_GET_BY_CONF_TYPE_REQUEST, SessionForms,
                      path='conference/session/get_by_conf_and_type/'
                           + '{websafeConferenceKey}/{typeOfSession}',
                      http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Get Sessions by conference key and typeOfSession"""
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        conf = c_key.get()
        # Check if conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'
                % request.websafeSessionKey)
        # Filter sessions with given type of session
        sessions = Session.query(ancestor=c_key).filter(
            Session.typeOfSession == request.typeOfSession).fetch()
        return SessionForms(
            items=[self._copySessionToForm(item) for item in sessions]
        )

    @endpoints.method(SPEAKER_GET_REQUEST, SessionForms,
                      path='conference/session/get_by_speaker/'
                           + '{websafeSpeakerKey}',
                      http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Get Sessions by speaker key"""
        speakerKey = ndb.Key(urlsafe=request.websafeSpeakerKey)
        speaker = speakerKey.get()
        # Check if speaker exists
        if not speaker:
            raise endpoints.NotFoundException(
                'No speaker found with key: %s' % request.websafeSpeakerKey)

        # Query sessions with given speaker key
        sessions = Session.query(
            Session.speakerKey == request.websafeSpeakerKey).fetch()
        # return SessionForms object
        return SessionForms(
            items=[self._copySessionToForm(item) for item in sessions]
        )

    # Task 2
    # - - - Wishlist - - - - - - - - - - - - - - - - - - - -

    @endpoints.method(SESSION_GET_REQUEST, BooleanMessage,
                      path='wishlist/add',
                      http_method='POST', name='addSessionToWishlist')
    @login_required
    @ndb.transactional(xg=True)
    def addSessionToWishlist(self, request):
        """Add Session to Wishlist by Session key"""
        prof = self._getProfileFromUser()

        session_key = ndb.Key(urlsafe=request.websafeSessionKey)
        session = session_key.get()
        # Check if session exists
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.websafeSessionKey)
        # Add session to user profile
        try:
            if request.websafeSessionKey in prof.sessionKeysOnWishlist:
                raise ConflictException(
                    "You already have this in your wishlist"
                )
            # Add session to wishlist
            prof.sessionKeysOnWishlist.append(request.websafeSessionKey)
            prof.put()
            result = True
        except:
            result = False
        # return BooleanMessage
        return BooleanMessage(data=result)

    @endpoints.method(SESSION_GET_REQUEST, BooleanMessage,
                      path='deleteSessionFromWishlist',
                      http_method='POST', name='deleteSessionFromWishlist')
    @login_required
    @ndb.transactional(xg=True)
    def deleteSessionFromWishlist(self, request):
        """Delete Session from Wishlist by session key"""
        prof = self._getProfileFromUser()
        # Check if session exists
        session_key = ndb.Key(urlsafe=request.websafeSessionKey)
        session = session_key.get()
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % request.websafeSessionKey)
        result = False
        try:
            # If session key in wishlist remove it, else raise exception
            if request.websafeSessionKey in prof.sessionKeysOnWishlist:
                prof.sessionKeysOnWishlist.remove(request.websafeSessionKey)
                prof.put()
                result = True

            else:
                raise endpoints.NotFoundException(
                    'No session found in wishlist with key: %s' % request.websafeSessionKey)
        except:
            result = False

        return BooleanMessage(data=result)

    @endpoints.method(message_types.VoidMessage, SessionForms,
                      path='wishlist',
                      http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get Sessions from wishlist"""
        prof = self._getProfileFromUser()
        return SessionForms(
            items=[self._copySessionToForm(
                ndb.Key(urlsafe=item).get())
                   for item in prof.sessionKeysOnWishlist]
        )

    # Task 3
    # - - - Additional Queries - - - - - - - - - - - - - - - - - - - -
    @endpoints.method(message_types.VoidMessage, SpeakerForms,
                      path='speaker/get_best_speakers',
                      http_method='GET', name='getBestSpeakers')
    def getSpeakersWithMostSessions(self, request):
        """Get Speakers with most sessions"""
        # Get top 5 featured speakers
        speakers = Speaker.query(
            Speaker.sessions_count > 0).order(-Speaker.sessions_count) \
            .fetch(5)
        return SpeakerForms(
            items=[self._copySpeakerToForm(item) for item in speakers]
        )

    @endpoints.method(message_types.VoidMessage, SessionForms,
                      path='conference/session/get_upcoming_sessions',
                      http_method='GET', name='getUpcomingSessions')
    def getUpcomingSessions(self, request):
        """Get Upcoming Sessions """
        # Get current date and time
        startTime = datetime.now()
        # Filter session by current date and get nearest 5 records
        sessions = Session.query().filter(
            Session.date >= startTime).order(Session.date).fetch(5)
        return SessionForms(
            items=[self._copySessionToForm(item) for item in sessions]
        )

    # - - - Problem query - - - - - - - - - - - - - - - - - - - -
    @endpoints.method(SESSION_GET_BY_TYPE_TIME_REQUEST, SessionForms,
                      path='conference/session/get_by_not_like',
                      http_method='POST', name='getSessionsByNotLike')
    def getSessionsByNotLike(self, request):
        """Get Sessions with other then chosen typeOfSession and before startTime """
        # Get start time
        start_time = datetime.strptime(request.startTime, "%H:%M").time()
        # Get type of sessions which user wants to attend
        only_types_sessions = Session.query(
            projection=[Session.typeOfSession],
            distinct=True).filter(
            Session.typeOfSession != request.typeOfSession).fetch()
        types = [sessions.typeOfSession for sessions in only_types_sessions]
        # Filter sessions with type and start time
        sessions = Session.query(
            Session.typeOfSession.IN(types)) \
            .filter(Session.startTime <= start_time).fetch()
        return SessionForms(
            items=[self._copySessionToForm(item) for item in sessions]
        )

    # Task 4
    # - - - Memcache speaker - - - - - - - - - - - - - - - - - - - -
    @staticmethod
    def _updateSpeaker(request):
        """Update speaker with new Session. """
        result = False
        try:
            # Get Speaker
            speakerKey = request.get('speakerKey')
            speaker = ndb.Key(urlsafe=speakerKey).get()
            if speaker:
                # Append Session to Speaker
                speaker.sessions.append(request.get('sessionName'))
                # Change sessions count to new number
                speaker.sessions_count = len(speaker.sessions)
                # Save changes
                speaker.put()
                result = True
        except Exception, e:
            print e
        return result

    @endpoints.method(message_types.VoidMessage, StringMessage,
                      path='speaker/get_featured_speaker',
                      http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return featured speaker from Memcache."""
        return StringMessage(
            data=memcache.get(MEMCACHE_FEATURED_SPEAKER) or "")


api = endpoints.api_server([ConferenceApi])  # register API
