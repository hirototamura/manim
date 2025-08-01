"""Basic canvas for animations."""

from __future__ import annotations

from manim.utils.parameter_parsing import flatten_iterable_parameters

from ..mobject.mobject import _AnimationBuilder

__all__ = ["Scene"]

import copy
import datetime
import inspect
import platform
import random
import threading
import time
from queue import Queue

import srt

from manim.scene.section import DefaultSectionType

try:
    import dearpygui.dearpygui as dpg

    dearpygui_imported = True
except ImportError:
    dearpygui_imported = False
from typing import TYPE_CHECKING

import numpy as np
from tqdm import tqdm
from watchdog.events import DirModifiedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from manim.mobject.mobject import Mobject
from manim.mobject.opengl.opengl_mobject import OpenGLPoint

from .. import config, logger
from ..animation.animation import Animation, Wait, prepare_animation
from ..camera.camera import Camera
from ..constants import *
from ..gui.gui import configure_pygui
from ..renderer.cairo_renderer import CairoRenderer
from ..renderer.opengl_renderer import OpenGLCamera, OpenGLMobject, OpenGLRenderer
from ..renderer.shader import Object3D
from ..utils import opengl, space_ops
from ..utils.exceptions import EndSceneEarlyException, RerunSceneException
from ..utils.family import extract_mobject_family_members
from ..utils.family_ops import restructure_list_to_exclude_certain_family_members
from ..utils.file_ops import open_media_file
from ..utils.iterables import list_difference_update, list_update

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from types import FrameType
    from typing import Any, Callable, TypeAlias

    from typing_extensions import Self

    from manim.typing import Point3D

    SceneInteractAction: TypeAlias = tuple[str, Iterable[Any], dict[str, Any]]
    """
    The SceneInteractAction type alias is used for elements in the queue
    used by Scene.interact().
    The elements consist consist of:

    - a string, which is either the name of a Scene method or some special keyword
      starting with "rerun" or "exit",
    - a list of args for the Scene method (only used if the first string actually
      corresponds to a method) and
    - a dict of kwargs for the Scene method (if the first string corresponds to one.
      Otherwise, currently Scene.interact() extracts a possible "from_animation_number" from it if the first string starts with "rerun"),
    as seen around the source code where it's common to use self.queue.put((method_name, [], {})) and similar items.

    """


class RerunSceneHandler(FileSystemEventHandler):
    """A class to handle rerunning a Scene after the input file is modified."""

    def __init__(self, queue: Queue[SceneInteractAction]) -> None:
        super().__init__()
        self.queue = queue

    def on_modified(self, event: DirModifiedEvent | FileModifiedEvent) -> None:
        self.queue.put(("rerun_file", [], {}))


class Scene:
    """A Scene is the canvas of your animation.

    The primary role of :class:`Scene` is to provide the user with tools to manage
    mobjects and animations.  Generally speaking, a manim script consists of a class
    that derives from :class:`Scene` whose :meth:`Scene.construct` method is overridden
    by the user's code.

    Mobjects are displayed on screen by calling :meth:`Scene.add` and removed from
    screen by calling :meth:`Scene.remove`.  All mobjects currently on screen are kept
    in :attr:`Scene.mobjects`.  Animations are played by calling :meth:`Scene.play`.

    A :class:`Scene` is rendered internally by calling :meth:`Scene.render`.  This in
    turn calls :meth:`Scene.setup`, :meth:`Scene.construct`, and
    :meth:`Scene.tear_down`, in that order.

    It is not recommended to override the ``__init__`` method in user Scenes.  For code
    that should be ran before a Scene is rendered, use :meth:`Scene.setup` instead.

    Examples
    --------
    Override the :meth:`Scene.construct` method with your code.

    .. code-block:: python

        class MyScene(Scene):
            def construct(self):
                self.play(Write(Text("Hello World!")))

    """

    def __init__(
        self,
        renderer: CairoRenderer | OpenGLRenderer | None = None,
        camera_class: type[Camera] = Camera,
        always_update_mobjects: bool = False,
        random_seed: int | None = None,
        skip_animations: bool = False,
    ) -> None:
        self.camera_class = camera_class
        self.always_update_mobjects = always_update_mobjects
        self.random_seed = random_seed
        self.skip_animations = skip_animations

        self.animations: list[Animation] | None = None
        self.stop_condition: Callable[[], bool] | None = None
        self.moving_mobjects: list[Mobject] = []
        self.static_mobjects: list[Mobject] = []
        self.time_progression: tqdm[float] | None = None
        self.duration: float | None = None
        self.last_t = 0.0
        self.queue: Queue[SceneInteractAction] = Queue()
        self.skip_animation_preview = False
        self.meshes: list[Object3D] = []
        self.camera_target = ORIGIN
        self.widgets: list[Any] = []
        self.dearpygui_imported = dearpygui_imported
        self.updaters: list[Callable[[float], None]] = []
        self.key_to_function_map: dict[str, Callable[[], None]] = {}
        self.mouse_press_callbacks: list[Callable[[], None]] = []
        self.interactive_mode = False

        if config.renderer == RendererType.OPENGL:
            # Items associated with interaction
            self.mouse_point = OpenGLPoint()
            self.mouse_drag_point = OpenGLPoint()
            if renderer is None:
                renderer = OpenGLRenderer()

        if renderer is None:
            self.renderer: CairoRenderer | OpenGLRenderer = CairoRenderer(
                # TODO: Is it a suitable approach to make an instance of
                # the self.camera_class here?
                camera_class=self.camera_class,
                skip_animations=self.skip_animations,
            )
        else:
            self.renderer = renderer
        self.renderer.init_scene(self)

        self.mobjects: list[Mobject] = []
        # TODO, remove need for foreground mobjects
        self.foreground_mobjects: list[Mobject] = []
        if self.random_seed is not None:
            random.seed(self.random_seed)
            np.random.seed(self.random_seed)

    @property
    def camera(self) -> Camera | OpenGLCamera:
        return self.renderer.camera

    @property
    def time(self) -> float:
        """The time since the start of the scene."""
        return self.renderer.time

    def __deepcopy__(self, clone_from_id: dict[int, Any]) -> Scene:
        cls = self.__class__
        result = cls.__new__(cls)
        clone_from_id[id(self)] = result
        for k, v in self.__dict__.items():
            if k in ["renderer", "time_progression"]:
                continue
            if k == "camera_class":
                setattr(result, k, v)
            setattr(result, k, copy.deepcopy(v, clone_from_id))

        return result

    def render(self, preview: bool = False) -> bool:
        """
        Renders this Scene.

        Parameters
        ---------
        preview
            If true, opens scene in a file viewer.
        """
        self.setup()
        try:
            self.construct()
        except EndSceneEarlyException:
            pass
        except RerunSceneException:
            self.remove(*self.mobjects)
            # TODO: The CairoRenderer does not have the method clear_screen()
            self.renderer.clear_screen()  # type: ignore[union-attr]
            self.renderer.num_plays = 0
            return True
        self.tear_down()
        # We have to reset these settings in case of multiple renders.
        self.renderer.scene_finished(self)

        # Show info only if animations are rendered or to get image
        if (
            self.renderer.num_plays
            or config["format"] == "png"
            or config["save_last_frame"]
        ):
            logger.info(
                f"Rendered {str(self)}\nPlayed {self.renderer.num_plays} animations",
            )

        # If preview open up the render after rendering.
        if preview:
            config["preview"] = True

        if config["preview"] or config["show_in_file_browser"]:
            open_media_file(self.renderer.file_writer)

        return False

    def setup(self) -> None:
        """
        This is meant to be implemented by any scenes which
        are commonly subclassed, and have some common setup
        involved before the construct method is called.
        """
        pass

    def tear_down(self) -> None:
        """
        This is meant to be implemented by any scenes which
        are commonly subclassed, and have some common method
        to be invoked before the scene ends.
        """
        pass

    def construct(self) -> None:
        """Add content to the Scene.

        From within :meth:`Scene.construct`, display mobjects on screen by calling
        :meth:`Scene.add` and remove them from screen by calling :meth:`Scene.remove`.
        All mobjects currently on screen are kept in :attr:`Scene.mobjects`.  Play
        animations by calling :meth:`Scene.play`.

        Notes
        -----
        Initialization code should go in :meth:`Scene.setup`.  Termination code should
        go in :meth:`Scene.tear_down`.

        Examples
        --------
        A typical manim script includes a class derived from :class:`Scene` with an
        overridden :meth:`Scene.construct` method:

        .. code-block:: python

            class MyScene(Scene):
                def construct(self):
                    self.play(Write(Text("Hello World!")))

        See Also
        --------
        :meth:`Scene.setup`
        :meth:`Scene.render`
        :meth:`Scene.tear_down`

        """
        pass  # To be implemented in subclasses

    def next_section(
        self,
        name: str = "unnamed",
        section_type: str = DefaultSectionType.NORMAL,
        skip_animations: bool = False,
    ) -> None:
        """Create separation here; the last section gets finished and a new one gets created.
        ``skip_animations`` skips the rendering of all animations in this section.
        Refer to :doc:`the documentation</tutorials/output_and_config>` on how to use sections.
        """
        self.renderer.file_writer.next_section(name, section_type, skip_animations)

    def __str__(self) -> str:
        return self.__class__.__name__

    def get_attrs(self, *keys: str) -> list[Any]:
        """
        Gets attributes of a scene given the attribute's identifier/name.

        Parameters
        ----------
        *keys
            Name(s) of the argument(s) to return the attribute of.

        Returns
        -------
        list
            List of attributes of the passed identifiers.
        """
        return [getattr(self, key) for key in keys]

    def update_mobjects(self, dt: float) -> None:
        """
        Begins updating all mobjects in the Scene.

        Parameters
        ----------
        dt
            Change in time between updates. Defaults (mostly) to 1/frames_per_second
        """
        for mobj in self.mobjects:
            mobj.update(dt)

    def update_meshes(self, dt: float) -> None:
        for obj in self.meshes:
            for mesh in obj.get_family():  # type: ignore[no-untyped-call]
                mesh.update(dt)

    def update_self(self, dt: float) -> None:
        """Run all scene updater functions.

        Among all types of update functions (mobject updaters, mesh updaters,
        scene updaters), scene update functions are called last.

        Parameters
        ----------
        dt
            Scene time since last update.

        See Also
        --------
        :meth:`.Scene.add_updater`
        :meth:`.Scene.remove_updater`
        """
        for func in self.updaters:
            func(dt)

    def should_update_mobjects(self) -> bool:
        """
        Returns True if the mobjects of this scene should be updated.

        In particular, this checks whether

        - the :attr:`always_update_mobjects` attribute of :class:`.Scene`
          is set to ``True``,
        - the :class:`.Scene` itself has time-based updaters attached,
        - any mobject in this :class:`.Scene` has time-based updaters attached.

        This is only called when a single Wait animation is played.
        """
        assert self.animations is not None
        wait_animation = self.animations[0]
        assert isinstance(wait_animation, Wait)
        if wait_animation.is_static_wait is None:
            should_update = (
                self.always_update_mobjects
                or self.updaters
                or wait_animation.stop_condition is not None
                or any(
                    mob.has_time_based_updater()
                    for mob in self.get_mobject_family_members()
                )
            )
            wait_animation.is_static_wait = not should_update
        return not wait_animation.is_static_wait

    def get_top_level_mobjects(self) -> list[Mobject]:
        """
        Returns all mobjects which are not submobjects.

        Returns
        -------
        list
            List of top level mobjects.
        """
        # Return only those which are not in the family
        # of another mobject from the scene
        families = [m.get_family() for m in self.mobjects]

        def is_top_level(mobject: Mobject) -> bool:
            num_families = sum((mobject in family) for family in families)
            return num_families == 1

        return list(filter(is_top_level, self.mobjects))

    def get_mobject_family_members(self) -> list[Mobject]:
        """
        Returns list of family-members of all mobjects in scene.
        If a Circle() and a VGroup(Rectangle(),Triangle()) were added,
        it returns not only the Circle(), Rectangle() and Triangle(), but
        also the VGroup() object.

        Returns
        -------
        list
            List of mobject family members.
        """
        if config.renderer == RendererType.OPENGL:
            family_members = []
            for mob in self.mobjects:
                family_members.extend(mob.get_family())
            return family_members
        else:
            assert config.renderer == RendererType.CAIRO
            return extract_mobject_family_members(
                self.mobjects,
                use_z_index=self.renderer.camera.use_z_index,
            )

    def add(self, *mobjects: Mobject | OpenGLMobject) -> Self:
        """
        Mobjects will be displayed, from background to
        foreground in the order with which they are added.

        Parameters
        ---------
        *mobjects
            Mobjects to add.

        Returns
        -------
        Scene
            The same scene after adding the Mobjects in.

        """
        if config.renderer == RendererType.OPENGL:
            new_mobjects = []
            new_meshes: list[Object3D] = []
            for mobject_or_mesh in mobjects:
                if isinstance(mobject_or_mesh, Object3D):
                    new_meshes.append(mobject_or_mesh)
                else:
                    new_mobjects.append(mobject_or_mesh)
            self.remove(*new_mobjects)  # type: ignore[arg-type]
            self.mobjects += new_mobjects  # type: ignore[arg-type]
            self.remove(*new_meshes)  # type: ignore[arg-type]
            self.meshes += new_meshes
        else:
            assert config.renderer == RendererType.CAIRO
            new_and_foreground_mobjects: list[Mobject] = [
                *mobjects,  # type: ignore[list-item]
                *self.foreground_mobjects,
            ]
            self.restructure_mobjects(to_remove=new_and_foreground_mobjects)
            self.mobjects += new_and_foreground_mobjects
            if self.moving_mobjects:
                self.restructure_mobjects(
                    to_remove=new_and_foreground_mobjects,
                    mobject_list_name="moving_mobjects",
                )
                self.moving_mobjects += new_and_foreground_mobjects
        return self

    def add_mobjects_from_animations(self, animations: list[Animation]) -> None:
        curr_mobjects = self.get_mobject_family_members()
        for animation in animations:
            if animation.is_introducer():
                continue
            # Anything animated that's not already in the
            # scene gets added to the scene
            mob = animation.mobject
            if mob is not None and mob not in curr_mobjects:
                self.add(mob)
                curr_mobjects += mob.get_family()  # type: ignore[arg-type]

    def remove(self, *mobjects: Mobject) -> Self:
        """
        Removes mobjects in the passed list of mobjects
        from the scene and the foreground, by removing them
        from "mobjects" and "foreground_mobjects"

        Parameters
        ----------
        *mobjects
            The mobjects to remove.
        """
        if config.renderer == RendererType.OPENGL:
            mobjects_to_remove = []
            meshes_to_remove: set[Object3D] = set()
            mobject_or_mesh: Mobject
            for mobject_or_mesh in mobjects:
                if isinstance(mobject_or_mesh, Object3D):
                    meshes_to_remove.add(mobject_or_mesh)
                else:
                    mobjects_to_remove.append(mobject_or_mesh)
            self.mobjects = restructure_list_to_exclude_certain_family_members(
                self.mobjects,
                mobjects_to_remove,
            )

            def lambda_function(mesh: Object3D) -> bool:
                return mesh not in set(meshes_to_remove)

            self.meshes = list(
                filter(lambda_function, self.meshes),
            )
            return self
        else:
            assert config.renderer == RendererType.CAIRO
            for list_name in "mobjects", "foreground_mobjects":
                self.restructure_mobjects(mobjects, list_name, False)
            return self

    def replace(self, old_mobject: Mobject, new_mobject: Mobject) -> None:
        """Replace one mobject in the scene with another, preserving draw order.

        If ``old_mobject`` is a submobject of some other Mobject (e.g. a
        :class:`.Group`), the new_mobject will replace it inside the group,
        without otherwise changing the parent mobject.

        Parameters
        ----------
        old_mobject
            The mobject to be replaced. Must be present in the scene.
        new_mobject
            A mobject which must not already be in the scene.

        """
        if old_mobject is None or new_mobject is None:
            raise ValueError("Specified mobjects cannot be None")

        def replace_in_list(
            mobj_list: list[Mobject], old_m: Mobject, new_m: Mobject
        ) -> bool:
            # We use breadth-first search because some Mobjects get very deep and
            # we expect top-level elements to be the most common targets for replace.
            for i in range(0, len(mobj_list)):
                # Is this the old mobject?
                if mobj_list[i] == old_m:
                    # If so, write the new object to the same spot and stop looking.
                    mobj_list[i] = new_m
                    return True
            # Now check all the children of all these mobs.
            for mob in mobj_list:  # noqa: SIM110
                if replace_in_list(mob.submobjects, old_m, new_m):
                    # If we found it in a submobject, stop looking.
                    return True
            # If we did not find the mobject in the mobject list or any submobjects,
            # (or the list was empty), indicate we did not make the replacement.
            return False

        # Make use of short-circuiting conditionals to check mobjects and then
        # foreground_mobjects
        replaced = replace_in_list(
            self.mobjects, old_mobject, new_mobject
        ) or replace_in_list(self.foreground_mobjects, old_mobject, new_mobject)

        if not replaced:
            raise ValueError(f"Could not find {old_mobject} in scene")

    def add_updater(self, func: Callable[[float], None]) -> None:
        """Add an update function to the scene.

        The scene updater functions are run every frame,
        and they are the last type of updaters to run.

        .. WARNING::

            When using the Cairo renderer, scene updaters that
            modify mobjects are not detected in the same way
            that mobject updaters are. To be more concrete,
            a mobject only modified via a scene updater will
            not necessarily be added to the list of *moving
            mobjects* and thus might not be updated every frame.

            TL;DR: Use mobject updaters to update mobjects.

        Parameters
        ----------
        func
            The updater function. It takes a float, which is the
            time difference since the last update (usually equal
            to the frame rate).

        See also
        --------
        :meth:`.Scene.remove_updater`
        :meth:`.Scene.update_self`
        """
        self.updaters.append(func)

    def remove_updater(self, func: Callable[[float], None]) -> None:
        """Remove an update function from the scene.

        Parameters
        ----------
        func
            The updater function to be removed.

        See also
        --------
        :meth:`.Scene.add_updater`
        :meth:`.Scene.update_self`
        """
        self.updaters = [f for f in self.updaters if f is not func]

    def restructure_mobjects(
        self,
        to_remove: Sequence[Mobject],
        mobject_list_name: str = "mobjects",
        extract_families: bool = True,
    ) -> Scene:
        """
        tl:wr
            If your scene has a Group(), and you removed a mobject from the Group,
            this dissolves the group and puts the rest of the mobjects directly
            in self.mobjects or self.foreground_mobjects.

        In cases where the scene contains a group, e.g. Group(m1, m2, m3), but one
        of its submobjects is removed, e.g. scene.remove(m1), the list of mobjects
        will be edited to contain other submobjects, but not m1, e.g. it will now
        insert m2 and m3 to where the group once was.

        Parameters
        ----------
        to_remove
            The Mobject to remove.

        mobject_list_name
            The list of mobjects ("mobjects", "foreground_mobjects" etc) to remove from.

        extract_families
            Whether the mobject's families should be recursively extracted.

        Returns
        -------
        Scene
            The Scene mobject with restructured Mobjects.
        """
        if extract_families:
            to_remove = extract_mobject_family_members(
                to_remove,
                use_z_index=self.renderer.camera.use_z_index,
            )
        _list = getattr(self, mobject_list_name)
        new_list = self.get_restructured_mobject_list(_list, to_remove)
        setattr(self, mobject_list_name, new_list)
        return self

    def get_restructured_mobject_list(
        self, mobjects: Iterable[Mobject], to_remove: Iterable[Mobject]
    ) -> list[Mobject]:
        """
        Given a list of mobjects and a list of mobjects to be removed, this
        filters out the removable mobjects from the list of mobjects.

        Parameters
        ----------

        mobjects
            The Mobjects to check.

        to_remove
            The list of mobjects to remove.

        Returns
        -------
        list
            The list of mobjects with the mobjects to remove removed.
        """
        new_mobjects: list[Mobject] = []

        def add_safe_mobjects_from_list(
            list_to_examine: Iterable[Mobject], set_to_remove: set[Mobject]
        ) -> None:
            for mob in list_to_examine:
                if mob in set_to_remove:
                    continue
                intersect = set_to_remove.intersection(mob.get_family())
                if intersect:
                    add_safe_mobjects_from_list(mob.submobjects, intersect)
                else:
                    new_mobjects.append(mob)

        add_safe_mobjects_from_list(mobjects, set(to_remove))
        return new_mobjects

    # TODO, remove this, and calls to this
    def add_foreground_mobjects(self, *mobjects: Mobject) -> Scene:
        """
        Adds mobjects to the foreground, and internally to the list
        foreground_mobjects, and mobjects.

        Parameters
        ----------
        *mobjects
            The Mobjects to add to the foreground.

        Returns
        ------
        Scene
            The Scene, with the foreground mobjects added.
        """
        self.foreground_mobjects = list_update(self.foreground_mobjects, mobjects)
        self.add(*mobjects)
        return self

    def add_foreground_mobject(self, mobject: Mobject) -> Scene:
        """
        Adds a single mobject to the foreground, and internally to the list
        foreground_mobjects, and mobjects.

        Parameters
        ----------
        mobject
            The Mobject to add to the foreground.

        Returns
        ------
        Scene
            The Scene, with the foreground mobject added.
        """
        return self.add_foreground_mobjects(mobject)

    def remove_foreground_mobjects(self, *to_remove: Mobject) -> Scene:
        """
        Removes mobjects from the foreground, and internally from the list
        foreground_mobjects.

        Parameters
        ----------
        *to_remove
            The mobject(s) to remove from the foreground.

        Returns
        ------
        Scene
            The Scene, with the foreground mobjects removed.
        """
        self.restructure_mobjects(to_remove, "foreground_mobjects")
        return self

    def remove_foreground_mobject(self, mobject: Mobject) -> Scene:
        """
        Removes a single mobject from the foreground, and internally from the list
        foreground_mobjects.

        Parameters
        ----------
        mobject
            The mobject to remove from the foreground.

        Returns
        ------
        Scene
            The Scene, with the foreground mobject removed.
        """
        return self.remove_foreground_mobjects(mobject)

    def bring_to_front(self, *mobjects: Mobject) -> Scene:
        """
        Adds the passed mobjects to the scene again,
        pushing them to he front of the scene.

        Parameters
        ----------
        *mobjects
            The mobject(s) to bring to the front of the scene.

        Returns
        ------
        Scene
            The Scene, with the mobjects brought to the front
            of the scene.
        """
        self.add(*mobjects)
        return self

    def bring_to_back(self, *mobjects: Mobject) -> Scene:
        """
        Removes the mobject from the scene and
        adds them to the back of the scene.

        Parameters
        ----------
        *mobjects
            The mobject(s) to push to the back of the scene.

        Returns
        ------
        Scene
            The Scene, with the mobjects pushed to the back
            of the scene.
        """
        self.remove(*mobjects)
        self.mobjects = list(mobjects) + self.mobjects
        return self

    def clear(self) -> Self:
        """
        Removes all mobjects present in self.mobjects
        and self.foreground_mobjects from the scene.

        Returns
        ------
        Scene
            The Scene, with all of its mobjects in
            self.mobjects and self.foreground_mobjects
            removed.
        """
        self.mobjects = []
        self.foreground_mobjects = []
        return self

    def get_moving_mobjects(self, *animations: Animation) -> list[Mobject]:
        """
        Gets all moving mobjects in the passed animation(s).

        Parameters
        ----------
        *animations
            The animations to check for moving mobjects.

        Returns
        ------
        list
            The list of mobjects that could be moving in
            the Animation(s)
        """
        # Go through mobjects from start to end, and
        # as soon as there's one that needs updating of
        # some kind per frame, return the list from that
        # point forward.
        animation_mobjects = [anim.mobject for anim in animations]
        mobjects = self.get_mobject_family_members()
        for i, mob in enumerate(mobjects):
            update_possibilities = [
                mob in animation_mobjects,
                len(mob.get_family_updaters()) > 0,
                mob in self.foreground_mobjects,
            ]
            if any(update_possibilities):
                return mobjects[i:]
        return []

    def get_moving_and_static_mobjects(
        self, animations: Iterable[Animation]
    ) -> tuple[list[Mobject], list[Mobject]]:
        all_mobjects = list_update(self.mobjects, self.foreground_mobjects)
        all_mobject_families = extract_mobject_family_members(
            all_mobjects,
            use_z_index=self.renderer.camera.use_z_index,
            only_those_with_points=True,
        )
        moving_mobjects = self.get_moving_mobjects(*animations)
        all_moving_mobject_families = extract_mobject_family_members(
            moving_mobjects,
            use_z_index=self.renderer.camera.use_z_index,
        )
        static_mobjects = list_difference_update(
            all_mobject_families,
            all_moving_mobject_families,
        )
        return all_moving_mobject_families, static_mobjects

    def compile_animations(
        self,
        *args: Animation | Mobject | _AnimationBuilder,
        **kwargs: Any,
    ) -> list[Animation]:
        """
        Creates _MethodAnimations from any _AnimationBuilders and updates animation
        kwargs with kwargs passed to play().

        Parameters
        ----------
        *args
            Animations to be played.
        **kwargs
            Configuration for the call to play().

        Returns
        -------
        Tuple[:class:`Animation`]
            Animations to be played.
        """
        animations = []
        arg_anims = flatten_iterable_parameters(args)
        # Allow passing a generator to self.play instead of comma separated arguments
        for arg in arg_anims:
            try:
                animations.append(prepare_animation(arg))  # type: ignore[arg-type]
            except TypeError as e:
                if inspect.ismethod(arg):
                    raise TypeError(
                        "Passing Mobject methods to Scene.play is no longer"
                        " supported. Use Mobject.animate instead.",
                    ) from e
                else:
                    raise TypeError(
                        f"Unexpected argument {arg} passed to Scene.play().",
                    ) from e

        for animation in animations:
            for k, v in kwargs.items():
                setattr(animation, k, v)

        return animations

    def _get_animation_time_progression(
        self, animations: list[Animation], duration: float
    ) -> tqdm[float]:
        """
        You will hardly use this when making your own animations.
        This method is for Manim's internal use.

        Uses :func:`~.get_time_progression` to obtain a
        CommandLine ProgressBar whose ``fill_time`` is
        dependent on the qualities of the passed Animation,

        Parameters
        ----------
        animations
            The list of animations to get
            the time progression for.

        duration
            duration of wait time

        Returns
        -------
        time_progression
            The CommandLine Progress Bar.
        """
        if len(animations) == 1 and isinstance(animations[0], Wait):
            stop_condition = animations[0].stop_condition
            if stop_condition is not None:
                time_progression = self.get_time_progression(
                    duration,
                    f"Waiting for {stop_condition.__name__}",
                    n_iterations=-1,  # So it doesn't show % progress
                    override_skip_animations=True,
                )
            else:
                time_progression = self.get_time_progression(
                    duration,
                    f"Waiting {self.renderer.num_plays}",
                )
        else:
            time_progression = self.get_time_progression(
                duration,
                "".join(
                    [
                        f"Animation {self.renderer.num_plays}: ",
                        str(animations[0]),
                        (", etc." if len(animations) > 1 else ""),
                    ],
                ),
            )
        return time_progression

    def get_time_progression(
        self,
        run_time: float,
        description: str,
        n_iterations: int | None = None,
        override_skip_animations: bool = False,
    ) -> tqdm[float]:
        """
        You will hardly use this when making your own animations.
        This method is for Manim's internal use.

        Returns a CommandLine ProgressBar whose ``fill_time``
        is dependent on the ``run_time`` of an animation,
        the iterations to perform in that animation
        and a bool saying whether or not to consider
        the skipped animations.

        Parameters
        ----------
        run_time
            The ``run_time`` of the animation.

        n_iterations
            The number of iterations in the animation.

        override_skip_animations
            Whether or not to show skipped animations in the progress bar.

        Returns
        -------
        time_progression
            The CommandLine Progress Bar.
        """
        if self.renderer.skip_animations and not override_skip_animations:
            times: Iterable[float] = [run_time]
        else:
            step = 1 / config["frame_rate"]
            times = np.arange(0, run_time, step)
        time_progression = tqdm(
            times,
            desc=description,
            total=n_iterations,
            leave=config["progress_bar"] == "leave",
            ascii=True if platform.system() == "Windows" else None,
            disable=config["progress_bar"] == "none",
        )
        return time_progression

    @classmethod
    def validate_run_time(
        cls,
        run_time: float,
        method: Callable[[Any], Any],
        parameter_name: str = "run_time",
    ) -> float:
        method_name = f"{cls.__name__}.{method.__name__}()"
        if run_time <= 0:
            raise ValueError(
                f"{method_name} has a {parameter_name} of "
                f"{run_time:g} <= 0 seconds which Manim cannot render. "
                f"The {parameter_name} must be a positive number."
            )

        # config.frame_rate holds the number of frames per second
        fps = config.frame_rate
        seconds_per_frame = 1 / fps
        if run_time < seconds_per_frame:
            logger.warning(
                f"The original {parameter_name} of {method_name}, "
                f"{run_time:g} seconds, is too short for the current frame "
                f"rate of {fps:g} FPS. Rendering with the shortest possible "
                f"{parameter_name} of {seconds_per_frame:g} seconds instead."
            )
            run_time = seconds_per_frame

        return run_time

    def get_run_time(self, animations: list[Animation]) -> float:
        """
        Gets the total run time for a list of animations.

        Parameters
        ----------
        animations
            A list of the animations whose total
            ``run_time`` is to be calculated.

        Returns
        -------
        float
            The total ``run_time`` of all of the animations in the list.
        """
        run_time = max(animation.run_time for animation in animations)
        run_time = self.validate_run_time(run_time, self.play, "total run_time")
        return run_time

    def play(
        self,
        *args: Animation | Mobject | _AnimationBuilder,
        subcaption: str | None = None,
        subcaption_duration: float | None = None,
        subcaption_offset: float = 0,
        **kwargs: Any,
    ) -> None:
        r"""Plays an animation in this scene.

        Parameters
        ----------

        args
            Animations to be played.
        subcaption
            The content of the external subcaption that should
            be added during the animation.
        subcaption_duration
            The duration for which the specified subcaption is
            added. If ``None`` (the default), the run time of the
            animation is taken.
        subcaption_offset
            An offset (in seconds) for the start time of the
            added subcaption.
        kwargs
            All other keywords are passed to the renderer.

        """
        # If we are in interactive embedded mode, make sure this is running on the main thread (required for OpenGL)
        if (
            self.interactive_mode
            and config.renderer == RendererType.OPENGL
            and threading.current_thread().name != "MainThread"
        ):
            kwargs.update(
                {
                    "subcaption": subcaption,
                    "subcaption_duration": subcaption_duration,
                    "subcaption_offset": subcaption_offset,
                }
            )
            self.queue.put(
                (
                    "play",
                    args,
                    kwargs,
                )
            )
            return

        start_time = self.time
        self.renderer.play(self, *args, **kwargs)
        run_time = self.time - start_time
        if subcaption:
            if subcaption_duration is None:
                subcaption_duration = run_time
            # The start of the subcaption needs to be offset by the
            # run_time of the animation because it is added after
            # the animation has already been played (and Scene.time
            # has already been updated).
            self.add_subcaption(
                content=subcaption,
                duration=subcaption_duration,
                offset=-run_time + subcaption_offset,
            )

    def wait(
        self,
        duration: float = DEFAULT_WAIT_TIME,
        stop_condition: Callable[[], bool] | None = None,
        frozen_frame: bool | None = None,
    ) -> None:
        """Plays a "no operation" animation.

        Parameters
        ----------
        duration
            The run time of the animation.
        stop_condition
            A function without positional arguments that is evaluated every time
            a frame is rendered. The animation only stops when the return value
            of the function is truthy, or when the time specified in ``duration``
            passes.
        frozen_frame
            If True, updater functions are not evaluated, and the animation outputs
            a frozen frame. If False, updater functions are called and frames
            are rendered as usual. If None (the default), the scene tries to
            determine whether or not the frame is frozen on its own.

        See also
        --------
        :class:`.Wait`, :meth:`.should_mobjects_update`
        """
        duration = self.validate_run_time(duration, self.wait, "duration")
        self.play(
            Wait(
                run_time=duration,
                stop_condition=stop_condition,
                frozen_frame=frozen_frame,
            )
        )

    def pause(self, duration: float = DEFAULT_WAIT_TIME) -> None:
        """Pauses the scene (i.e., displays a frozen frame).

        This is an alias for :meth:`.wait` with ``frozen_frame``
        set to ``True``.

        Parameters
        ----------
        duration
            The duration of the pause.

        See also
        --------
        :meth:`.wait`, :class:`.Wait`
        """
        duration = self.validate_run_time(duration, self.pause, "duration")
        self.wait(duration=duration, frozen_frame=True)

    def wait_until(
        self, stop_condition: Callable[[], bool], max_time: float = 60
    ) -> None:
        """Wait until a condition is satisfied, up to a given maximum duration.

        Parameters
        ----------
        stop_condition
            A function with no arguments that determines whether or not the
            scene should keep waiting.
        max_time
            The maximum wait time in seconds.
        """
        max_time = self.validate_run_time(max_time, self.wait_until, "max_time")
        self.wait(max_time, stop_condition=stop_condition)

    def compile_animation_data(
        self,
        *animations: Animation | Mobject | _AnimationBuilder,
        **play_kwargs: Any,
    ) -> Self | None:
        """Given a list of animations, compile the corresponding
        static and moving mobjects, and gather the animation durations.

        This also begins the animations.

        Parameters
        ----------
        animations
            Animation or mobject with mobject method and params
        play_kwargs
            Named parameters affecting what was passed in ``animations``,
            e.g. ``run_time``, ``lag_ratio`` and so on.

        Returns
        -------
        self, None
            None if there is nothing to play, or self otherwise.
        """
        # NOTE TODO : returns statement of this method are wrong. It should return nothing, as it makes a little sense to get any information from this method.
        # The return are kept to keep webgl renderer from breaking.
        if len(animations) == 0:
            raise ValueError("Called Scene.play with no animations")

        self.animations = self.compile_animations(*animations, **play_kwargs)
        self.add_mobjects_from_animations(self.animations)

        self.last_t = 0
        self.stop_condition = None
        self.moving_mobjects = []
        self.static_mobjects = []

        self.duration = self.get_run_time(self.animations)
        if len(self.animations) == 1 and isinstance(self.animations[0], Wait):
            if self.should_update_mobjects():
                self.update_mobjects(dt=0)  # Any problems with this?
                self.stop_condition = self.animations[0].stop_condition
            else:
                # Static image logic when the wait is static is done by the renderer, not here.
                self.animations[0].is_static_wait = True
                return None

        return self

    def begin_animations(self) -> None:
        """Start the animations of the scene."""
        assert self.animations is not None
        for animation in self.animations:
            animation._setup_scene(self)
            animation.begin()

        if config.renderer == RendererType.CAIRO:
            # Paint all non-moving objects onto the screen, so they don't
            # have to be rendered every frame
            (
                self.moving_mobjects,
                self.static_mobjects,
            ) = self.get_moving_and_static_mobjects(self.animations)

    def is_current_animation_frozen_frame(self) -> bool:
        """Returns whether the current animation produces a static frame (generally a Wait)."""
        assert self.animations is not None
        return (
            isinstance(self.animations[0], Wait)
            and len(self.animations) == 1
            and self.animations[0].is_static_wait
        )

    def play_internal(self, skip_rendering: bool = False) -> None:
        """
        This method is used to prep the animations for rendering,
        apply the arguments and parameters required to them,
        render them, and write them to the video file.

        Parameters
        ----------
        skip_rendering
            Whether the rendering should be skipped, by default False
        """
        assert self.animations is not None
        self.duration = self.get_run_time(self.animations)
        self.time_progression = self._get_animation_time_progression(
            self.animations,
            self.duration,
        )
        for t in self.time_progression:
            self.update_to_time(t)
            if not skip_rendering and not self.skip_animation_preview:
                self.renderer.render(self, t, self.moving_mobjects)
            if self.stop_condition is not None and self.stop_condition():
                self.time_progression.close()
                break

        for animation in self.animations:
            animation.finish()
            animation.clean_up_from_scene(self)
        if not self.renderer.skip_animations:
            self.update_mobjects(0)
        # TODO: The OpenGLRenderer does not have the property static.image.
        self.renderer.static_image = None  # type: ignore[union-attr]
        # Closing the progress bar at the end of the play.
        self.time_progression.close()

    def check_interactive_embed_is_valid(self) -> bool:
        assert isinstance(self.renderer, OpenGLRenderer)
        if config["force_window"]:
            return True
        if self.skip_animation_preview:
            logger.warning(
                "Disabling interactive embed as 'skip_animation_preview' is enabled",
            )
            return False
        elif config["write_to_movie"]:
            logger.warning("Disabling interactive embed as 'write_to_movie' is enabled")
            return False
        elif config["format"]:
            logger.warning(
                "Disabling interactive embed as '--format' is set as "
                + config["format"],
            )
            return False
        elif not self.renderer.window:
            logger.warning("Disabling interactive embed as no window was created")
            return False
        elif config.dry_run:
            logger.warning("Disabling interactive embed as dry_run is enabled")
            return False
        return True

    def interactive_embed(self) -> None:
        """Like embed(), but allows for screen interaction."""
        assert isinstance(self.camera, OpenGLCamera)
        assert isinstance(self.renderer, OpenGLRenderer)
        if not self.check_interactive_embed_is_valid():
            return
        self.interactive_mode = True
        from IPython.terminal.embed import InteractiveShellEmbed

        def ipython(shell: InteractiveShellEmbed, namespace: dict[str, Any]) -> None:
            import manim.opengl

            def load_module_into_namespace(
                module: Any, namespace: dict[str, Any]
            ) -> None:
                for name in dir(module):
                    namespace[name] = getattr(module, name)

            load_module_into_namespace(manim, namespace)
            load_module_into_namespace(manim.opengl, namespace)

            def embedded_rerun(*args: Any, **kwargs: Any) -> None:
                self.queue.put(("rerun_keyboard", args, kwargs))
                shell.exiter()

            namespace["rerun"] = embedded_rerun

            shell(local_ns=namespace)
            self.queue.put(("exit_keyboard", [], {}))

        def get_embedded_method(method_name: str) -> Callable[..., None]:
            def embedded_method(*args: Any, **kwargs: Any) -> None:
                self.queue.put((method_name, args, kwargs))

            return embedded_method

        currentframe: FrameType = inspect.currentframe()  # type: ignore[assignment]
        local_namespace = currentframe.f_back.f_locals  # type: ignore[union-attr]
        for method in ("play", "wait", "add", "remove"):
            embedded_method = get_embedded_method(method)
            # Allow for calling scene methods without prepending 'self.'.
            local_namespace[method] = embedded_method

        from sqlite3 import connect

        from IPython.core.getipython import get_ipython
        from traitlets.config import Config

        cfg = Config()
        cfg.TerminalInteractiveShell.confirm_exit = False
        if get_ipython() is None:
            shell = InteractiveShellEmbed.instance(config=cfg)
        else:
            shell = InteractiveShellEmbed(config=cfg)
        hist = get_ipython().history_manager
        hist.db = connect(hist.hist_file, check_same_thread=False)

        keyboard_thread = threading.Thread(
            target=ipython,
            args=(shell, local_namespace),
        )
        # run as daemon to kill thread when main thread exits
        if not shell.pt_app:
            keyboard_thread.daemon = True
        keyboard_thread.start()

        if self.dearpygui_imported and config["enable_gui"]:
            if not dpg.is_dearpygui_running():
                gui_thread = threading.Thread(
                    target=configure_pygui,
                    args=(self.renderer, self.widgets),
                    kwargs={"update": False},
                )
                gui_thread.start()
            else:
                configure_pygui(self.renderer, self.widgets, update=True)

        self.camera.model_matrix = self.camera.default_model_matrix

        self.interact(shell, keyboard_thread)

    # from IPython.terminal.embed import InteractiveShellEmbed

    def interact(self, shell: Any, keyboard_thread: threading.Thread) -> None:
        assert isinstance(self.renderer, OpenGLRenderer)
        event_handler = RerunSceneHandler(self.queue)
        file_observer = Observer()
        file_observer.schedule(event_handler, config["input_file"], recursive=True)
        file_observer.start()

        self.quit_interaction = False
        keyboard_thread_needs_join = shell.pt_app is not None
        assert self.queue.qsize() == 0

        last_time = time.time()
        while not (self.renderer.window.is_closing or self.quit_interaction):
            if not self.queue.empty():
                tup = self.queue.get_nowait()
                if tup[0].startswith("rerun"):
                    # Intentionally skip calling join() on the file thread to save time.
                    if not tup[0].endswith("keyboard"):
                        if shell.pt_app:
                            shell.pt_app.app.exit(exception=EOFError)
                        file_observer.unschedule_all()
                        raise RerunSceneException
                    keyboard_thread.join()

                    kwargs = tup[2]
                    if "from_animation_number" in kwargs:
                        config["from_animation_number"] = kwargs[
                            "from_animation_number"
                        ]
                    # # TODO: This option only makes sense if interactive_embed() is run at the
                    # # end of a scene by default.
                    # if "upto_animation_number" in kwargs:
                    #     config["upto_animation_number"] = kwargs[
                    #         "upto_animation_number"
                    #     ]

                    keyboard_thread.join()
                    file_observer.unschedule_all()
                    raise RerunSceneException
                elif tup[0].startswith("exit"):
                    # Intentionally skip calling join() on the file thread to save time.
                    if not tup[0].endswith("keyboard") and shell.pt_app:
                        shell.pt_app.app.exit(exception=EOFError)
                    keyboard_thread.join()
                    # Remove exit_keyboard from the queue if necessary.
                    while self.queue.qsize() > 0:
                        self.queue.get()
                    keyboard_thread_needs_join = False
                    break
                else:
                    method, args, kwargs = tup
                    getattr(self, method)(*args, **kwargs)
            else:
                self.renderer.animation_start_time = 0
                dt = time.time() - last_time
                last_time = time.time()
                self.renderer.render(self, dt, self.moving_mobjects)
                self.update_mobjects(dt)
                self.update_meshes(dt)
                self.update_self(dt)

        # Join the keyboard thread if necessary.
        if shell is not None and keyboard_thread_needs_join:
            shell.pt_app.app.exit(exception=EOFError)
            keyboard_thread.join()
            # Remove exit_keyboard from the queue if necessary.
            while self.queue.qsize() > 0:
                self.queue.get()

        file_observer.stop()
        file_observer.join()

        if self.dearpygui_imported and config["enable_gui"]:
            dpg.stop_dearpygui()

        if self.renderer.window.is_closing:
            self.renderer.window.destroy()

    def embed(self) -> None:
        assert isinstance(self.renderer, OpenGLRenderer)
        if not config["preview"]:
            logger.warning("Called embed() while no preview window is available.")
            return
        if config["write_to_movie"]:
            logger.warning("embed() is skipped while writing to a file.")
            return

        self.renderer.animation_start_time = 0
        self.renderer.render(self, -1, self.moving_mobjects)

        # Configure IPython shell.
        from IPython.terminal.embed import InteractiveShellEmbed

        shell = InteractiveShellEmbed()

        # Have the frame update after each command
        shell.events.register(
            "post_run_cell",
            lambda *a, **kw: self.renderer.render(self, -1, self.moving_mobjects),
        )

        # Use the locals of the caller as the local namespace
        # once embedded, and add a few custom shortcuts.
        current_frame = inspect.currentframe()
        assert isinstance(current_frame, FrameType)
        local_ns = current_frame.f_back.f_locals  # type: ignore[union-attr]
        # local_ns["touch"] = self.interact
        for method in (
            "play",
            "wait",
            "add",
            "remove",
            "interact",
            # "clear",
            # "save_state",
            # "restore",
        ):
            local_ns[method] = getattr(self, method)
        shell(local_ns=local_ns, stack_depth=2)

        # End scene when exiting an embed.
        raise Exception("Exiting scene.")

    def update_to_time(self, t: float) -> None:
        dt = t - self.last_t
        self.last_t = t
        assert self.animations is not None
        for animation in self.animations:
            animation.update_mobjects(dt)
            alpha = t / animation.run_time
            animation.interpolate(alpha)
        self.update_mobjects(dt)
        self.update_meshes(dt)
        self.update_self(dt)

    def add_subcaption(
        self, content: str, duration: float = 1, offset: float = 0
    ) -> None:
        r"""Adds an entry in the corresponding subcaption file
        at the current time stamp.

        The current time stamp is obtained from ``Scene.time``.

        Parameters
        ----------

        content
            The subcaption content.
        duration
            The duration (in seconds) for which the subcaption is shown.
        offset
            This offset (in seconds) is added to the starting time stamp
            of the subcaption.

        Examples
        --------

        This example illustrates both possibilities for adding
        subcaptions to Manimations::

            class SubcaptionExample(Scene):
                def construct(self):
                    square = Square()
                    circle = Circle()

                    # first option: via the add_subcaption method
                    self.add_subcaption("Hello square!", duration=1)
                    self.play(Create(square))

                    # second option: within the call to Scene.play
                    self.play(
                        Transform(square, circle), subcaption="The square transforms."
                    )

        """
        subtitle = srt.Subtitle(
            index=len(self.renderer.file_writer.subcaptions),
            content=content,
            start=datetime.timedelta(seconds=float(self.time + offset)),
            end=datetime.timedelta(seconds=float(self.time + offset + duration)),
        )
        self.renderer.file_writer.subcaptions.append(subtitle)

    def add_sound(
        self,
        sound_file: str,
        time_offset: float = 0,
        gain: float | None = None,
        **kwargs: Any,
    ) -> None:
        """
        This method is used to add a sound to the animation.

        Parameters
        ----------

        sound_file
            The path to the sound file.
        time_offset
            The offset in the sound file after which
            the sound can be played.
        gain
            Amplification of the sound.

        Examples
        --------
        .. manim:: SoundExample
            :no_autoplay:

            class SoundExample(Scene):
                # Source of sound under Creative Commons 0 License. https://freesound.org/people/Druminfected/sounds/250551/
                def construct(self):
                    dot = Dot().set_color(GREEN)
                    self.add_sound("click.wav")
                    self.add(dot)
                    self.wait()
                    self.add_sound("click.wav")
                    dot.set_color(BLUE)
                    self.wait()
                    self.add_sound("click.wav")
                    dot.set_color(RED)
                    self.wait()

        Download the resource for the previous example `here <https://github.com/ManimCommunity/manim/blob/main/docs/source/_static/click.wav>`_ .
        """
        if self.renderer.skip_animations:
            return
        time = self.time + time_offset
        self.renderer.file_writer.add_sound(sound_file, time, gain, **kwargs)

    def on_mouse_motion(self, point: Point3D, d_point: Point3D) -> None:
        assert isinstance(self.camera, OpenGLCamera)
        assert isinstance(self.renderer, OpenGLRenderer)
        self.mouse_point.move_to(point)
        if SHIFT_VALUE in self.renderer.pressed_keys:
            shift = -d_point
            shift[0] *= self.camera.get_width() / 2
            shift[1] *= self.camera.get_height() / 2
            transform = self.camera.inverse_rotation_matrix
            shift = np.dot(np.transpose(transform), shift)
            self.camera.shift(shift)

    def on_mouse_scroll(self, point: Point3D, offset: Point3D) -> None:
        assert isinstance(self.camera, OpenGLCamera)
        if not config.use_projection_stroke_shaders:
            factor = 1 + np.arctan(-2.1 * offset[1])
            self.camera.scale(factor, about_point=self.camera_target)
        self.mouse_scroll_orbit_controls(point, offset)

    def on_key_press(self, symbol: int, modifiers: int) -> None:
        assert isinstance(self.camera, OpenGLCamera)
        try:
            char = chr(symbol)
        except OverflowError:
            logger.warning("The value of the pressed key is too large.")
            return

        if char == "r":
            self.camera.to_default_state()
            self.camera_target = np.array([0, 0, 0], dtype=np.float32)
        elif char == "q":
            self.quit_interaction = True
        else:
            if char in self.key_to_function_map:
                self.key_to_function_map[char]()

    def on_key_release(self, symbol: int, modifiers: int) -> None:
        pass

    def on_mouse_drag(
        self,
        point: Point3D,
        d_point: Point3D,
        buttons: int,
        modifiers: int,
    ) -> None:
        assert isinstance(self.camera, OpenGLCamera)
        self.mouse_drag_point.move_to(point)
        if buttons == 1:
            self.camera.increment_theta(-d_point[0])
            self.camera.increment_phi(d_point[1])
        elif buttons == 4:
            camera_x_axis = self.camera.model_matrix[:3, 0]
            horizontal_shift_vector = -d_point[0] * camera_x_axis
            vertical_shift_vector = -d_point[1] * np.cross(OUT, camera_x_axis)
            total_shift_vector = horizontal_shift_vector + vertical_shift_vector
            self.camera.shift(1.1 * total_shift_vector)

        self.mouse_drag_orbit_controls(point, d_point, buttons, modifiers)

    def mouse_scroll_orbit_controls(self, point: Point3D, offset: Point3D) -> None:
        assert isinstance(self.camera, OpenGLCamera)
        camera_to_target = self.camera_target - self.camera.get_position()
        camera_to_target *= np.sign(offset[1])
        shift_vector = 0.01 * camera_to_target
        self.camera.model_matrix = (
            opengl.translation_matrix(*shift_vector) @ self.camera.model_matrix
        )

    def mouse_drag_orbit_controls(
        self,
        point: Point3D,
        d_point: Point3D,
        buttons: int,
        modifiers: int,
    ) -> None:
        assert isinstance(self.camera, OpenGLCamera)
        # Left click drag.
        if buttons == 1:
            # Translate to target the origin and rotate around the z axis.
            self.camera.model_matrix = (
                opengl.rotation_matrix(z=-d_point[0])
                @ opengl.translation_matrix(*-self.camera_target)
                @ self.camera.model_matrix
            )

            # Rotation off of the z axis.
            camera_position = self.camera.get_position()
            camera_y_axis = self.camera.model_matrix[:3, 1]
            axis_of_rotation = space_ops.normalize(
                np.cross(camera_y_axis, camera_position),
            )
            rotation_matrix = space_ops.rotation_matrix(
                d_point[1],
                axis_of_rotation,
                homogeneous=True,
            )

            maximum_polar_angle = self.camera.maximum_polar_angle
            minimum_polar_angle = self.camera.minimum_polar_angle

            potential_camera_model_matrix = rotation_matrix @ self.camera.model_matrix
            potential_camera_location = potential_camera_model_matrix[:3, 3]
            potential_camera_y_axis = potential_camera_model_matrix[:3, 1]
            sign = (
                np.sign(potential_camera_y_axis[2])
                if potential_camera_y_axis[2] != 0
                else 1
            )
            potential_polar_angle = sign * np.arccos(
                potential_camera_location[2]
                / np.linalg.norm(potential_camera_location),
            )
            if minimum_polar_angle <= potential_polar_angle <= maximum_polar_angle:
                self.camera.model_matrix = potential_camera_model_matrix
            else:
                sign = np.sign(camera_y_axis[2]) if camera_y_axis[2] != 0 else 1
                current_polar_angle = sign * np.arccos(
                    camera_position[2] / np.linalg.norm(camera_position),
                )
                if potential_polar_angle > maximum_polar_angle:
                    polar_angle_delta = maximum_polar_angle - current_polar_angle
                else:
                    polar_angle_delta = minimum_polar_angle - current_polar_angle
                rotation_matrix = space_ops.rotation_matrix(
                    polar_angle_delta,
                    axis_of_rotation,
                    homogeneous=True,
                )
                self.camera.model_matrix = rotation_matrix @ self.camera.model_matrix

            # Translate to target the original target.
            self.camera.model_matrix = (
                opengl.translation_matrix(*self.camera_target)
                @ self.camera.model_matrix
            )
        # Right click drag.
        elif buttons == 4:
            camera_x_axis = self.camera.model_matrix[:3, 0]
            horizontal_shift_vector = -d_point[0] * camera_x_axis
            vertical_shift_vector = -d_point[1] * np.cross(OUT, camera_x_axis)
            total_shift_vector = horizontal_shift_vector + vertical_shift_vector

            self.camera.model_matrix = (
                opengl.translation_matrix(*total_shift_vector)
                @ self.camera.model_matrix
            )
            self.camera_target += total_shift_vector

    def set_key_function(self, char: str, func: Callable[[], Any]) -> None:
        self.key_to_function_map[char] = func

    def on_mouse_press(self, point: Point3D, button: int, modifiers: int) -> None:
        for func in self.mouse_press_callbacks:
            func()
