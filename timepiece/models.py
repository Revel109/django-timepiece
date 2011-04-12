import datetime
import logging
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Avg, Sum, Max, Min
from django.contrib.auth.models import User

from timepiece import utils

from dateutil.relativedelta import relativedelta
from dateutil import rrule

from datetime import timedelta

from crm import models as crm

from timepiece import utils

try:
    settings.TIMEPIECE_TIMESHEET_EDITABLE_DAYS
except AttributeError:
    settings.TIMEPIECE_TIMESHEET_EDITABLE_DAYS = 3

class Attribute(models.Model):
    ATTRIBUTE_TYPES = (
        ('project-type', 'Project Type'),
        ('project-status', 'Project Status'),
    )
    SORT_ORDER_CHOICES = [(x,x) for x in xrange(-20,21)]
    type = models.CharField(max_length=32, choices=ATTRIBUTE_TYPES)
    label = models.CharField(max_length=255)
    sort_order = models.SmallIntegerField(
        null=True,
        blank=True,
        choices=SORT_ORDER_CHOICES,
    )
    enable_timetracking = models.BooleanField('Enables time tracking '
        'functionality for projects with this type or status.',
        default=False,
    )

    class Meta:
        unique_together = ('type', 'label')
        ordering = ('sort_order',)

    def __unicode__(self):
        return self.label


class Project(models.Model):
    name = models.CharField(max_length = 255)
    trac_environment = models.CharField(max_length=255, blank=True, null=True)
    business = models.ForeignKey(
        crm.Contact,
        related_name='business_projects',
        limit_choices_to={'type': 'business'},
    )
    point_person = models.ForeignKey(User, limit_choices_to={'is_staff': True})
    contacts = models.ManyToManyField(
        crm.Contact,
        related_name='contact_projects',
        through='ProjectRelationship',
    )
    type = models.ForeignKey(
        Attribute,
        limit_choices_to={'type': 'project-type'},
        related_name='projects_with_type',
    )
    status = models.ForeignKey(
        Attribute,
        limit_choices_to={'type': 'project-status'},
        related_name='projects_with_status',
    )
    description = models.TextField()

    interactions = models.ManyToManyField(crm.Interaction, blank=True)

    billing_period = models.ForeignKey(
        'RepeatPeriod',
        null=True,
        blank=True,
        related_name='projects',
    )

    class Meta:
        ordering = ('name', 'status', 'type',)
        permissions = (
            ('view_project', 'Can view project'),
            ('email_project_report', 'Can email project report'),
            ('view_project_time_sheet', 'Can view project time sheet'),
            ('export_project_time_sheet', 'Can export project time sheet'),
        )

    def __unicode__(self):
        return self.name

    def trac_url(self):
        return settings.TRAC_URL % self.trac_environment


class ProjectRelationship(models.Model):
    types = models.ManyToManyField(
        crm.RelationshipType,
        related_name='project_relationships',
        blank=True,
    )
    contact = models.ForeignKey(
        crm.Contact,
        limit_choices_to={'type': 'individual'},
        related_name='project_relationships',
    )
    project = models.ForeignKey(
        Project,
        related_name='project_relationships',
    )

    class Meta:
        unique_together = ('contact', 'project')

    def __unicode__(self):
        return "%s's relationship to %s" % (
            self.project.name,
            self.contact.get_full_name(),
        )


class Activity(models.Model):
    """
    Represents different types of activity: debugging, developing,
    brainstorming, QA, etc...
    """
    code = models.CharField(
        max_length=5,
        unique=True,
        help_text="""Enter a short code to describe the type of activity that took place."""
    )
    name = models.CharField(
        max_length=50,
        help_text="""Now enter a more meaningful name for the activity.""",
    )

    def __unicode__(self):
        return self.name

    class Meta:
        ordering = ('name',)
        verbose_name_plural = 'activities'


class Location(models.Model):
    name = models.CharField(max_length=255, unique=True)
    slug = models.CharField(max_length=255, unique=True)

    def __unicode__(self):
        return self.name


class EntryWorkedManager(models.Manager):
    def get_query_set(self):
        qs = super(EntryWorkedManager, self).get_query_set()
        projects = getattr(settings, 'TIMEPIECE_PROJECTS', {})
        return qs.exclude(project__in=projects.values())


class Entry(models.Model):
    """
    This class is where all of the time logs are taken care of
    """

    user = models.ForeignKey(User, related_name='timepiece_entries')
    project = models.ForeignKey(Project, related_name='entries')
    activity = models.ForeignKey(
        Activity,
        blank=True,
        null=True,
        related_name='entries',
    )
    location = models.ForeignKey(
        Location,
        related_name='entries',
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(blank=True, null=True)
    seconds_paused = models.PositiveIntegerField(default=0)
    pause_time = models.DateTimeField(blank=True, null=True)
    comments = models.TextField(blank=True)
    date_updated = models.DateTimeField(auto_now=True)
    hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    billable = models.BooleanField(default=True)

    objects = models.Manager()
    worked = EntryWorkedManager()

    def is_overlapping(self):
        if self.start_time and self.end_time:
            entries = self.user.timepiece_entries.filter(
            Q(end_time__range=(self.start_time,self.end_time))|\
            Q(start_time__range=(self.start_time,self.end_time))|\
            Q(start_time__lte=self.start_time, end_time__gte=self.end_time))
            totals = entries.aggregate(
            max=Max('end_time'),min=Min('start_time'))
            totals['total'] = 0
            for entry in entries:
                totals['total'] = totals['total'] + entry.get_seconds()
            totals['diff'] = totals['max']-totals['min']
            totals['diff'] = totals['diff'].seconds + totals['diff'].days*86400
            if totals['total'] > totals['diff']:
                return True
            else:
                return False
        else:
            return None

    def save(self, **kwargs):
        self.hours = Decimal('%.2f' % round(self.total_hours, 2))
        super(Entry, self).save(**kwargs)

    def get_seconds(self):
        """
        Determines the difference between the starting and ending time.  The
        result is returned as an integer of seconds.
        """
        if self.start_time and self.end_time:
            # only calculate when the start and end are defined
            delta = self.end_time - self.start_time
            seconds = delta.seconds - self.seconds_paused
        else:
            seconds = 0
            delta = datetime.timedelta(days=0)

        return seconds + (delta.days * 86400)

    def __total_hours(self):
        """
        Determined the total number of hours worked in this entry
        """
        return self.get_seconds() / 3600.0
    total_hours = property(__total_hours)

    def __is_paused(self):
        """
        Determine whether or not this entry is paused
        """
        return bool(self.pause_time)
    is_paused = property(__is_paused)

    def pause(self):
        """
        If this entry is not paused, pause it.
        """
        if not self.is_paused:
            self.pause_time = datetime.datetime.now()

    def pause_all(self):
        """
        Pause all open entries
        """
        entries = self.user.timepiece_entries.filter(
        end_time__isnull=True).all()
        for entry in entries:
            entry.pause()
            entry.save()

    def unpause(self, date=None):
        if self.is_paused:
            self.pause_all()
            if not date:
                date = datetime.datetime.now()
            delta = date - self.pause_time
            self.seconds_paused += delta.seconds
            self.pause_time = None

    def toggle_paused(self):
        """
        Toggle the paused state of this entry.  If the entry is already paused,
        it will be unpaused; if it is not paused, it will be paused.
        """
        if self.is_paused:
            self.unpause()
        else:
            self.pause()

    def __is_closed(self):
        """
        Determine whether this entry has been closed or not
        """
        return bool(self.end_time)
    is_closed = property(__is_closed)

    def clock_in(self, user, project):
        """
        Set this entry up for saving the first time, as an open entry.
        """
        if not self.is_closed:
            self.user = user
            self.project = project
            self.pause_all()
            if not self.start_time:
                self.start_time = datetime.datetime.now()

    def __billing_window(self):
        return BillingWindow.objects.get(
            period__contacts__user=self.user,
            date__lte = self.end_time,
            end_date__gt = self.end_time)
    billing_window = property(__billing_window)

    def __is_editable(self):
        if self.end_time:
            try:
                end_date =self.billing_window.end_date+\
                    timedelta(days=settings.TIMEPIECE_TIMESHEET_EDITABLE_DAYS)
                return end_date >= datetime.date.today()
            except:
                return True
        else:
            return True
    is_editable = property(__is_editable)

    def __delete_key(self):
        """
        Make it a little more interesting for deleting logs
        """
        salt = '%i-%i-apple-%s-sauce' % (self.id, self.is_paused, self.is_closed)
        try:
            import hashlib
        except ImportError:
            import sha
            key = sha.new(salt).hexdigest()
        else:
            key = hashlib.sha1(salt).hexdigest()
        return key
    delete_key = property(__delete_key)

    def __unicode__(self):
        """
        The string representation of an instance of this class
        """
        return '%s on %s' % (self.user, self.project)

    class Meta:
        verbose_name_plural = 'entries'
        permissions = (
            ('can_clock_in', 'Can use Pendulum to clock in'),
            ('can_pause', 'Can pause and unpause log entries'),
            ('can_clock_out', 'Can use Pendulum to clock out'),
            ('view_entry_summary', 'Can view entry summary page'),
        )

# Add a utility method to the User class that will tell whether or not a
# particular user has any unclosed entries
User.clocked_in = property(lambda user: user.timepiece_entries.filter(end_time__isnull=True).count() > 0)


class RepeatPeriodManager(models.Manager):
    def update_billing_windows(self, date_boundary=None):
        active_billing_periods = self.filter(
            active=True,
        ).select_related(
            'project'
        )
        windows = []
        for period in active_billing_periods:
            windows += ((period, period.update_billing_windows(date_boundary)),)
        return windows


class RepeatPeriod(models.Model):
    INTERVAL_CHOICES = (
        ('day', 'Day(s)'),
        ('week', 'Week(s)'),
        ('month', 'Month(s)'),
        ('year', 'Year(s)'),
    )
    count = models.PositiveSmallIntegerField(
        choices=[(x,x) for x in range(1,32)],
    )
    interval = models.CharField(
        max_length=10,
        choices=INTERVAL_CHOICES,
    )
    active = models.BooleanField(default=False)

    contacts = models.ManyToManyField(
        crm.Contact,
        blank=True,
        through='PersonRepeatPeriod',
        related_name='repeat_periods',
    )

    objects = RepeatPeriodManager()

    def __unicode__(self):
        return "%d %s" % (self.count, self.get_interval_display())

    def delta(self):
        return relativedelta(**{str(self.interval + 's'): self.count})

    def update_billing_windows(self, date_boundary=None):
        if not date_boundary:
            date_boundary = datetime.date.today()
        windows = []
        try:
            window = self.billing_windows.order_by('-date').select_related()[0]
        except IndexError:
            window = None
        if window:
            start_date = window.date
            while window.date + self.delta() <= date_boundary:
                window.id = None
                if window.date + self.delta() == window.end_date:
                    # same delta as last time
                    window.date += self.delta()
                else:
                    # delta changed, make sure to include extra time
                    window.date = window.end_date
                window.end_date += self.delta()
                window.save(force_insert=True)
            return self.billing_windows.filter(
                date__gt=start_date
            ).order_by('date')
        else:
            return []

class BillingWindow(models.Model):
    period = models.ForeignKey(RepeatPeriod, related_name='billing_windows')
    date = models.DateField()
    end_date = models.DateField()

    class Meta:
        get_latest_by = 'date'

    def __unicode__(self):
        return "%s through %s" % (self.date, self.end_date)

    def next(self):
        if not hasattr(self, '_next'):
            try:
                window = BillingWindow.objects.filter(
                    period=self.period,
                    date__gt=self.date,
                ).order_by('date')[0]
            except IndexError:
                window = None
            self._next = window
        return self._next

    def previous(self):
        if not hasattr(self, '_previous'):
            try:
                window = BillingWindow.objects.filter(
                    period=self.period,
                    date__lt=self.date,
                ).order_by('-date')[0]
            except IndexError:
                window = None
            self._previous = window
        return self._previous

    def __entries(self):
            return Entry.objects.filter(
                end_time__lte = self.end_date,
                end_time__gt = self.date)
    entries = property(__entries)

class PersonRepeatPeriod(models.Model):
    contact = models.ForeignKey(
        crm.Contact,
        unique=True,
        limit_choices_to={'type': 'individual'}
    )
    repeat_period = models.ForeignKey(
        RepeatPeriod,
        unique=True,
    )

    def hours_in_week(self, date):
        left, right = utils.get_week_window(date)
        entries = Entry.worked.filter(user=self.contact.user)
        entries = entries.filter(end_time__gt=left, end_time__lt=right)
        return entries.aggregate(s=Sum('hours'))['s']

    def overtime_hours_in_week(self, date):
        hours = self.hours_in_week(date)
        if hours > 40:
            return hours - 40
        return 0

    def total_monthly_overtime(self, day):
        start = day.replace(day=1)
        end = start + relativedelta(months=1)
        weeks = utils.generate_weeks(start=start, end=end)
        overtime = Decimal('0.0')
        for week in weeks:
            overtime += self.overtime_hours_in_week(week)
        return overtime

    def summary(self, date, end_date):
        projects = getattr(settings, 'TIMEPIECE_PROJECTS', {})
        user = self.contact.user
        entries = user.timepiece_entries.filter(end_time__gt=date,
                                                end_time__lte=end_date)
        data = {}
        data['total'] = entries.aggregate(s=Sum('hours'))['s']
        billable = entries.exclude(project__in=projects.values())
        billable = billable.values('billable').annotate(s=Sum('hours'))
        for row in billable:
            if row['billable']:
                data['billable'] = row['s']
            else:
                data['non_billable'] = row['s']
        vacation = entries.filter(project=projects['vacation'])
        data['vacation'] = vacation.aggregate(s=Sum('hours'))['s']
        sick = entries.filter(project=projects['sick'])
        data['sick'] = sick.aggregate(s=Sum('hours'))['s']
        return data

    def list_total_hours(self, N = 2):
        bw = BillingWindow.objects.filter(period=self.repeat_period).order_by('-date')[:N]
        result = []
        for b in bw:
            result.append(self.contact.user.timepiece_entries.filter(
                end_time__lte = b.end_date,
                end_time__gt = b.date
            ).aggregate(total=Sum('hours')))
        return result


class ProjectContract(models.Model):
    CONTRACT_STATUS = (
        ('upcoming', 'Upcoming'),
        ('current', 'Current'),
        ('complete', 'Complete'),
    )

    project = models.ForeignKey(Project, related_name='contracts')
    start_date = models.DateField()
    end_date = models.DateField()
    num_hours = models.DecimalField(max_digits=8, decimal_places=2,
                                    default=0)
    status = models.CharField(choices=CONTRACT_STATUS, default='upcomming',
                              max_length=32)

    def hours_worked(self):
        # TODO put this in a .extra w/a subselect
        if not hasattr(self, '_hours_worked'):
            self._hours_worked = Entry.objects.filter(
                project=self.project,
                start_time__gte=self.start_date,
                end_time__lt=self.end_date + datetime.timedelta(days=1),
            ).aggregate(sum=Sum('hours'))['sum']
        return self._hours_worked or 0

    @property
    def hours_assigned(self):
        # TODO put this in a .extra w/a subselect
        if not hasattr(self, '_hours_assigned'):
            self._hours_assigned =\
              self.assignments.aggregate(sum=Sum('num_hours'))['sum']
        return self._hours_assigned or 0

    @property
    def hours_allocated(self):
        allocations = AssignmentAllocation.objects.filter(assignment__contract=self)
        return allocations.aggregate(sum=Sum('hours'))['sum']

    @property
    def hours_remaining(self):
        return self.num_hours - self.hours_worked()

    @property
    def weeks_remaining(self):
        return utils.generate_weeks(end=self.end_date)

    def __unicode__(self):
        return unicode(self.project)


class AssignmentManager(models.Manager):
    def active_during_week(self, week, next_week):
        q = Q(contract__end_date__gte=week, contract__end_date__lt=next_week)
        q |= Q(contract__start_date__gte=week, contract__start_date__lt=next_week)
        q |= Q(contract__start_date__lt=week, contract__end_date__gt=next_week)
        return self.get_query_set().filter(q)

    def sort_by_priority(self):
        return sorted(self.get_query_set().all(), key=lambda contract: contract.this_weeks_priority_number)


# contract assignment logger
logger = logging.getLogger('timepiece.ca')


class ContractAssignment(models.Model):
    contract = models.ForeignKey(ProjectContract, related_name='assignments')
    contact = models.ForeignKey(
        crm.Contact,
        limit_choices_to={'type': 'individual'},
        related_name='assignments',
    )
    start_date = models.DateField()
    end_date = models.DateField()
    num_hours = models.DecimalField(max_digits=8, decimal_places=2,
                                    default=0)
    min_hours_per_week = models.IntegerField(default=0)

    objects = AssignmentManager()

    def _log(self, msg):
        logger.debug('{0} - {1}'.format(self, msg))

    def _filtered_hours_worked(self, end_date):
        return Entry.objects.filter(
            user=self.contact.user,
            project=self.contract.project,
            start_time__gte=self.start_date,
            end_time__lt=end_date,
        ).aggregate(sum=Sum('hours'))['sum'] or 0

    def filtered_hours_worked_with_in_window(self, start_date, end_date):
        return Entry.objects.filter(
            user=self.contact.user,
            project=self.contract.project,
            start_time__gte=start_date,
            end_time__lt=end_date,
        ).aggregate(sum=Sum('hours'))['sum'] or 0

    @property
    def hours_worked(self):
        if not hasattr(self, '_hours_worked'):
            date = self.end_date + datetime.timedelta(days=1)
            self._hours_worked = self._filtered_hours_worked(date)
        return self._hours_worked or 0

    @property
    def hours_remaining(self):
        return self.num_hours - self.hours_worked

    @property
    def this_weeks_priority_number(self):
        """
        Only works if already filtered to the current week. Otherwise groups
        outside the range will be listed as ongoing instead of befor or after.
        """
        if not hasattr(self, '_priority_type'):
            weeks = utils.get_week_window(datetime.datetime.now())
            if self.end_date < weeks[1].date() and self.end_date >= weeks[0].date():
                self._priority_type = 0
            elif self.start_date < weeks[1].date() and self.start_date >= weeks[0].date():
                self._priority_type = 1
            else:
                self._priority_type = 2
        return self._priority_type

    @property
    def this_weeks_priority_type(self):
        type_list = ['ending', 'starting', 'ongoing',]
        return type_list[self.this_weeks_priority_number]

    def get_average_weekly_committment(self):
        week_start = utils.get_week_start()
        # calculate hours left on contract (subtract worked hours this week)
        remaining = self.num_hours - self._filtered_hours_worked(week_start)
        commitment = remaining/self.contract.weeks_remaining.count()
        return commitment

    def weekly_commitment(self, day=None):
        self._log("Commitment for {0}".format(day))
        # earlier assignments may have already allocated time for this week
        unallocated = self.unallocated_hours_for_week(day)
        self._log('Unallocated hours {0}'.format(unallocated))
        reserved = self.remaining_min_hours()
        self._log('Reserved hours {0}'.format(reserved))
        # start with unallocated hours
        commitment = unallocated
        # reserve required hours on later assignments (min_hours_per_week)
        commitment -= self.remaining_min_hours()
        self._log('Commitment after reservation {0}'.format(commitment))
        # if we're under the needed minimum hours and we have available
        # time, then raise our commitment to the desired level
        if commitment < self.min_hours_per_week and unallocated >= self.min_hours_per_week:
            commitment = self.min_hours_per_week
        self._log('Commitment after minimum weekly hours {0}'.format(commitment))
        # calculate hours left on contract (subtract worked hours this week)
        week_start = utils.get_week_start(day)
        remaining = self.num_hours - self._filtered_hours_worked(week_start)
        total_allocated = self.blocks.aggregate(s=Sum('hours'))['s'] or 0
        remaining -= total_allocated
        if remaining < 0:
            remaining = 0
        self._log('Remaining {0}'.format(remaining))
        # reduce commitment to remaining hours
        if commitment > remaining:
            commitment = remaining
        self._log('Final commitment {0}'.format(commitment))
        return commitment

    def allocated_hours_for_week(self, day):
        week, next_week = utils.get_week_window(day)
        allocs = AssignmentAllocation.objects
        allocs = allocs.filter(assignment__contact=self.contact)
        allocs = allocs.filter(date__gte=week, date__lt=next_week)
        hours = allocs.aggregate(s=Sum('hours'))['s']
        return hours or 0

    def unallocated_hours_for_week(self, day):
        """ Calculate number of hours left to work for a week """
        allocated = self.allocated_hours_for_week(day)
        self._log('Allocated hours {0}'.format(allocated))
        try:
            schedule = PersonSchedule.objects.filter(contact=self.contact)[0]
        except IndexError:
            schedule = None
        if schedule:
            unallocated = schedule.hours_per_week - allocated
        else:
            unallocated = 40 - allocated
        return unallocated

    def remaining_contracts(self):
        assignments = ContractAssignment.objects.exclude(pk=self.pk)
        assignments = assignments.filter(end_date__gte=self.end_date,
                                         contact=self.contact)
        return assignments.order_by('-end_date')

    def remaining_min_hours(self):
        return self.remaining_contracts().aggregate(s=Sum('min_hours_per_week'))['s'] or 0

    class Meta:
        unique_together = (('contract', 'contact'),)

    def __unicode__(self):
        return u'%s / %s' % (self.contact, self.contract.project)


class AllocationManager(models.Manager):
    def during_this_week(self, user, day=None):
        week = utils.get_week_start(day=day)
        return self.get_query_set().filter(
            date=week, assignment__contact__user=user
            ).exclude(hours=0)


class AssignmentAllocation(models.Model):
    assignment = models.ForeignKey(ContractAssignment, related_name='blocks')
    date = models.DateField()
    hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    @property
    def hours_worked(self):
        if not hasattr(self, '_hours_worked'):
            end_date = self.date + datetime.timedelta(weeks=1)
            self._hours_worked = self.assignment.\
                    filtered_hours_worked_with_in_window(self.date, end_date)
        return self._hours_worked or 0

    @property
    def hours_left(self):
        if not hasattr(self, '_hours_left'):
            self._hours_left = self.hours - self.hours_worked
        return self._hours_left or 0

    objects = AllocationManager()


class PersonSchedule(models.Model):
    contact = models.ForeignKey(
        crm.Contact,
        unique=True,
        limit_choices_to={'type': 'individual'}
    )
    hours_per_week = models.DecimalField(max_digits=8, decimal_places=2,
                                         default=0)
    end_date = models.DateField()

    @property
    def furthest_end_date(self):
        assignments = self.contact.assignments.order_by('-end_date')
        assignments = assignments.exclude(contract__status='complete')
        try:
            end_date = assignments.values('end_date')[0]['end_date']
        except IndexError:
            end_date = self.end_date
        return end_date

    @property
    def hours_available(self):
        today = datetime.date.today()
        weeks_remaining = (self.end_date - today).days/7.0
        return float(self.hours_per_week) * weeks_remaining

    @property
    def hours_scheduled(self):
        if not hasattr(self, '_hours_scheduled'):
            self._hours_scheduled = 0
            now = datetime.datetime.now()
            for assignment in self.contact.assignments.filter(end_date__gte=now):
                self._hours_scheduled += assignment.hours_remaining
        return self._hours_scheduled

    def __unicode__(self):
        return unicode(self.contact)

