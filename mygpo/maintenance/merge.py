import restkit
from mygpo.core.models import Podcast, Episode
from mygpo.users.models import PodcastUserState
from mygpo import utils
from mygpo.decorators import repeat_on_conflict


def merge_objects(podcasts=True, podcast_states=False, episodes=False, episode_states=False, dry_run=False):
    """
    Merges objects (podcasts, podcast states, episodes) based on different criteria
    """

    # The "smaller" podcast is merged into the "greater"
    podcast_merge_order = lambda a, b: cmp(a.subscriber_count(), b.subscriber_count())
    no_merge_order = lambda a, b: 0

    if podcasts:

        print 'Merging Podcasts by URL'
        podcasts, total = get_view_count_iter(Podcast, 'core/podcasts_by_url')
        merge_from_iterator(podcasts, similar_urls, podcast_merge_order, total, merge_podcasts, dry_run)

        print 'Merging Podcasts by Old-Id'
        podcasts, total = get_view_count_iter(Podcast, 'core/podcasts_by_oldid')
        merge_from_iterator(podcasts, similar_oldid, podcast_merge_order, total, merge_podcasts, dry_run)


    if podcast_states:
        print 'Merging Duplicate Podcast States'
        states, total = get_view_count_iter(PodcastUserState, 'users/podcast_states_by_user')
        should_merge = lambda a, b: a == b
        merge_from_iterator(states, should_merge, no_merge_order, total, merge_podcast_states, dry_run)


    if episodes:
        print 'Merging Episodes by URL'
        episodes, total = get_view_count_iter(Episode, 'core/episodes_by_url', include_docs=True)
        should_merge = lambda a, b: a.podcast == b.podcast and similar_urls(a, b)
        merge_from_iterator(episodes, should_merge, no_merge_order, total, merge_episodes, dry_run)

    if episode_states:
        print 'Merging Episodes by Old-Id'
        episodes, total = get_view_count_iter(Episode, 'core/episodes_by_oldid', include_docs=True)
        should_merge = lambda a, b: a.podcast == b.podcast and similar_oldid(a, b)
        merge_from_iterator(episodes, should_merge, no_merge_order, total, merge_episodes, dry_run)


def get_view_count_iter(cls, view):
    iterator = utils.multi_request_view(cls, view)
    total = cls.view(view, limit=0).total_rows
    return iterator, total


def merge_from_iterator(obj_it, should_merge, cmp, total, merge_func, dry_run):
    """
    Iterates over the objects in obj_it and calls should_merge for each pair of
    objects. This implies that the objects returned by obj_it should be sorted
    such that potential merge-candiates appear after each other.

    If should_merge returns True, the pair of objects is going to be merged.
    The smaller object (according to cmp) is merged into the larger one.
    merge_func performs the actual merge. It is passed the two objects to be
    merged (first the larger, then the smaller one).
    """

    try:
        prev = obj_it.next()
    except StopIteration:
        return

    for n, p in enumerate(obj_it):
        if should_merge(p, prev):
            print 'merging %s, %s' % (p, prev)
            items = sorted([p, prev], cmp=cmp)
            merge_func(*items, dry_run=dry_run)

        prev = p
        utils.progress(n, total)


###
#
#  MERGING PODCASTS
#
###

def merge_podcasts(podcast, p, dry_run):
    """
    Merges p into podcast
    """

    @repeat_on_conflict(['podcast'], reload_f=lambda p: Podcast.get(p.get_id()))
    def do_merge(podcast, p):
        podcast.merged_ids       = list(set(podcast.merged_ids + [p.get_id()] + p.merged_ids))
        podcast.related_podcasts = list(set(podcast.related_podcasts + p.related_podcasts))
        podcast.content_types    = list(set(podcast.content_types + p.content_types))

        cmp_subscriber_entries = lambda a, b: cmp(a.timestamp, b.timestamp)
        for a, b in utils.iterate_together(podcast.subscribers, p.subscribers, cmp_subscriber_entries):
            if a is None or b is None: continue

            # avoid increasing subscriber_count when merging
            # duplicate entries of a single podcast
            if a.subscriber_count == b.subscriber_count: continue

            a.subscriber_count += b.subscriber_count

        for src, tags in p.tags.items():
            podcast.tags[src] = list(set(podcast.tags.get(src, []) + tags))

        if not dry_run:
            podcast.save()

    @repeat_on_conflict(['p'])
    def do_delete(p):
        if not dry_run:
            try:
                p.delete()
            except Exception as e:
                print repr(e), e


    # re-assign episodes to new podcast
    # if necessary, they will be merged later anyway
    for e in p.get_episodes():
        e.podcast = podcast.get_id()

        @repeat_on_conflict(['e'])
        def save_episode(e):
            if not dry_run:
                e.save()

        save_episode(e=e)


    do_merge(podcast=podcast, p=p)
    merge_podcast_states_for_podcasts(podcast, p, dry_run=dry_run)
    do_delete(p=p)


def similar_urls(a, b):
    """ Two Podcasts/Episodes are merged, if they have the same URLs"""
    return bool(utils.intersect(a.urls, b.urls))


def similar_oldid(o1, o2):
    """ Two Podcasts/Episodes are merged, if they have the same Old-IDs"""
    return o1.oldid == o2.oldid and o1.oldid is not None


###
#
# MERGING EPISODES
#
###


def merge_episodes(episode, e, dry_run):
    episode.urls = list(set(episode.urls + e.urls))
    episode.merged_ids = list(set(episode.merged_ids + [e.id] + e.merged_ids))

    @repeat_on_conflict(['e'])
    def delete(e):
        if not dry_run:
            e.delete()

    @repeat_on_conflict(['episode'])
    def save(episode):
        if not dry_run:
            episode.save()

    delete(e=e)
    save(podcast=podcast)

###
#
# MERGING PODCAST STATES
#
###

def merge_podcast_states_for_podcasts(p1, p2, dry_run):
    """Merges the Podcast states that are associated with the two Podcasts.

    This should be done after two podcasts are merged
    """

    @repeat_on_conflict(['s2'])
    def move(s2, new_id):
        s2.podcast = new_id
        if not dry_run:
            s2.save()

    @repeat_on_conflict(['s2'])
    def delete(s2):
        if not dry_run:
            s2.delete()

    cmp_states = lambda s1, s2: cmp(s1.user_oldid, s2.user_oldid)
    states1 = p1.get_all_states()
    states2 = p2.get_all_states()
    for s1, s2 in utils.iterate_together(states1, states2, cmp_states):
        if s1 == s2:
            continue

        if s1 == None:
            s2.ref_url = p1.url
            move(s2=s2, new_id=p1.get_id())

        elif s2 == None:
            continue

        else:
            if not s1.ref_url:
                s1.ref_url = p1.url
            merge_podcast_states(s1, s2, dry_run=dry_run)
            delete(s2=s2)


def merge_podcast_states(state1, state2, dry_run):
    """Merges the two given podcast states"""

    @repeat_on_conflict(['s1'])
    def do_merge(s1, s2):
        s1.settings = s2.settings.update(s1.settings)
        s1.episodes.update(s2.episodes)
        s1.disabled_devices = list(set(s1.disabled_devices + s2.disabled_devices))
        s1.merged_ids = list(set(s1.merged_ids + [s2._id] + s2.merged_ids))
        if not dry_run:
            s1.save()

    @repeat_on_conflict(['state'])
    def add_actions(state, actions):
        try:
            state.add_actions(actions)
            state.save()
        except restkit.Unauthorized:
            return

    @repeat_on_conflict(['state'])
    def add_action(state, action):
        try:
            state.add_actions([action])
            state.save()
        except restkit.Unauthorized:
            return

    do_merge(s1=state1, s2=state2)

    add_actions(state=state1, actions=state2.actions)

    for action in state2.actions:
        add_action(state=state1, action=action)



###
#
# MERGING EPISODE STATES
#
###

def merge_similar_episode_states(podcast, podcast_state):
    for e in podcast_state.episodes:
        for e2 in podcast_state.episodes:
            if e == e2: continue
            new_id = find_new_episode_id(podcast, e.id)
            if new_id != e.id:
                merge_episode_states(e, e2, podcast_state)


def find_new_episode_id(podcast, merged_id):
    for episode in podcast.get_episodes():
        if episode._id == merged_id:
            return merged_id

        if merged_id in episode.merged_ids:
            return episode._id

    return None


def merge_episode_states(state, other_state, podcast_state):
    state.add_actions(other_state.actions)
    state.settings.update(other_state.settings)
    state.merged_ids = list(set(state.merged_ids + [other_state._id] + other_state.merged_ids))
    if other_state.episode in podcast_state.episodes:
        del podcast_state.episodes[other_state.episode]