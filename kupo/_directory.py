from __future__ import annotations

import os
import re
from pathlib import Path
from subprocess import call

from rich.align import Align
from rich.console import RenderableType, RenderResult, Console, ConsoleOptions
from rich.markup import escape
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.dom import DOMNode
from textual.geometry import clamp, Size, Region
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from kupo._directory_search import DirectorySearch
from kupo._files import convert_size, list_files_in_dir, _count_files, rm_tree


class EmptyDirectoryRenderable:
    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield Align.center(Text.from_markup("[dim]─ Empty directory ─[/]"))


class DirectoryListRenderable:
    def __init__(
        self,
        files: list[Path],
        selected_index: int | None,
        filter: str = "",
        dir_style: Style | None = None,
        highlight_style: Style | None = None,
        highlight_dir_style: Style | None = None,
        meta_column_style: Style | None = None,
        highlight_meta_column_style: Style | None = None,
        chosen_path_style: Style | None = None,
        chosen_path_meta_style: Style | None = None,
        chosen_path_selected_style: Style | None = None,
        chosen_path_selected_meta_style: Style | None = None,
        chosen_paths: set[Path] | None = None,
    ) -> None:
        self.files = files
        self.selected_index = selected_index
        self.filter = filter
        self.dir_style = dir_style
        self.highlight_style = highlight_style
        self.highlight_dir_style = highlight_dir_style
        self.meta_column_style = meta_column_style
        self.highlight_meta_column_style = highlight_meta_column_style
        self.chosen_path_style = chosen_path_style
        self.chosen_path_meta_style = chosen_path_meta_style
        self.chosen_path_selected_style = chosen_path_selected_style
        self.chosen_path_selected_meta_style = chosen_path_selected_meta_style
        self.chosen_paths = chosen_paths

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        if not self.files:
            yield EmptyDirectoryRenderable()

        table = Table.grid(expand=True)
        table.add_column()
        table.add_column(justify="right", max_width=8)

        for index, file in enumerate(self.files):
            is_dir = file.is_dir()
            if not self.filter or (self.filter and re.search(self.filter, file.name)):

                if index == self.selected_index:
                    meta_style = self.highlight_meta_column_style
                    if is_dir:
                        style = self.highlight_dir_style or "bold red on #1E90FF"
                    else:
                        style = self.highlight_style or "bold red on #1E90FF"
                else:
                    meta_style = self.meta_column_style
                    if is_dir:
                        style = self.dir_style
                    else:
                        style = Style.null()

                if isinstance(style, str):
                    style = Style.parse(style)

                if self.chosen_paths and file in self.chosen_paths:
                    style += self.chosen_path_style
                    meta_style += self.chosen_path_meta_style
                    if index == self.selected_index:
                        style += self.chosen_path_selected_style
                        meta_style += self.chosen_path_selected_meta_style

                file_name = escape(file.name)
                if is_dir:
                    file_name += "/"
                    meta_value = str(_count_files(file) or "?")
                    meta_style += Style(dim=True)
                else:
                    try:
                        meta_value = convert_size(file.stat().st_size)
                    except FileNotFoundError:
                        meta_value = "[dim]?"

                file_name = Text(file_name, style=style)
                if file_name.plain.startswith(".") and index != self.selected_index:
                    file_name.stylize(Style(dim=True))
                if self.filter:
                    file_name.highlight_regex(self.filter, "#191004 on #FEA62B")

                table.add_row(
                    file_name,
                    Text.from_markup(meta_value, style=meta_style),
                )
        yield table


class Directory(Widget, can_focus=True):
    COMPONENT_CLASSES = {
        "directory--dir",
        "directory--highlighted",
        "directory--highlighted-dir",
        "directory--meta-column",
        "directory--highlighted-meta-column",
        "directory--chosen-path",
        "directory--chosen-path-meta",
        "directory--chosen-path-selected",
        "directory--chosen-path-selected-meta",
    }
    BINDINGS = [
        Binding("slash", "find", "Find", key_display="/"),
        Binding("escape", "clear_filter", "Clear", key_display="ESC"),
        Binding("l,right,enter", "choose_path", "In", key_display="l", show=False),
        Binding("h,left", "goto_parent", "Out", key_display="h", show=False),
        Binding("j,down", "next_file", "Next", key_display="j", show=False),
        Binding("k,up", "prev_file", "Prev", key_display="k", show=False),
        Binding("g", "first", "First", key_display="g", show=False),
        Binding("G", "last", "Last", show=False),
        Binding("space", "toggle_selected", "Toggle selected", key_display="space"),
        Binding("D", "delete_selected", "Delete selected", key_display="D"),
    ]

    filter = reactive("")

    def __init__(
        self,
        directory_search: DirectorySearch | None = None,
        cursor_movement_enabled: bool = False,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        path: Path | None = None,
    ):
        """
        Args:
            path: The Path of the directory to display contents of.
            chosen_paths: The set of chosen paths (secondary selections).
        """
        super().__init__(name=name, id=id, classes=classes)
        self.path = path or Path.cwd()
        self._files = list_files_in_dir(self.path)
        self.directory_search = directory_search
        self.cursor_movement_enabled = cursor_movement_enabled
        self.chosen_paths: set[Path] = set()

    def key_up(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.action_prev_file()

    def key_down(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self.action_next_file()

    def _on_mount(self, event: events.Mount) -> None:
        # This is in place to trigger the FilePreviewChanged
        self.selected_index = 0

    @property
    def selected_index(self):
        return self._selected_index

    @selected_index.setter
    def selected_index(self, new_value: int | None):

        if new_value is not None:
            self._selected_index = self._clamp_index(new_value)
            if self._files:
                selected_file = self._files[self._selected_index]
                self.emit_no_wait(Directory.FilePreviewChanged(self, selected_file))
        # If we're scrolled such that the selected index is not on screen.
        # That is, if the selected index does not lie between scroll_y and scroll_y+content_region.height,
        # Then update the scrolling
        self.refresh(layout=True)

    def action_find(self):
        self.directory_search.display = True
        self.directory_search.focus()

    def action_next_file(self):
        if self.has_focus and self.cursor_movement_enabled:
            self.selected_index += 1
            self.parent.scroll_to_region(Region(0, self.selected_index + 1),
                                         animate=False)

    def action_prev_file(self):
        if self.has_focus and self.cursor_movement_enabled:
            self.selected_index -= 1
            self.parent.scroll_to_region(Region(0, self.selected_index), animate=False)

    def action_clear_filter(self):
        if self.directory_search.input.value:
            self.directory_search.input.value = ""
            warning_banner = self.app.query_one("#current-dir-filter-warning")
            warning_banner.display = False
        self.chosen_paths.clear()
        self.refresh()
        self._emit_secondary_selection_changed()

    def action_first(self):
        self.selected_index = self._clamp_index(0)

    def action_last(self):
        self.selected_index = len(self._files) - 1 if self._files else None

    def action_choose_path(self):
        self.goto_selected_path()

    def goto_selected_path(self):
        if not self.current_highlighted_path:
            return
        if self.current_highlighted_path.is_dir():
            self.chosen_paths.clear()
            self.emit_no_wait(
                Directory.CurrentDirChanged(
                    self, new_dir=self.current_highlighted_path, from_dir=None
                )
            )
        elif self.current_highlighted_path.is_file():
            editor = os.environ.get('EDITOR', 'vim')
            with self.app.suspend():
                call([editor, str(self.current_highlighted_path.resolve().absolute())])

    def action_goto_parent(self):
        self.directory_search.input.value = ""
        self.emit_no_wait(
            Directory.CurrentDirChanged(
                self, new_dir=self.path.parent, from_dir=self.path
            )
        )

    def action_toggle_selected(self):
        if self.current_highlighted_path in self.chosen_paths:
            self.chosen_paths.remove(self.current_highlighted_path)
        else:
            self.chosen_paths.add(self.current_highlighted_path)
        self.refresh()
        self._emit_secondary_selection_changed()

    def action_delete_selected(self):
        print(f"removing selected files {self.chosen_paths}")
        chosen_paths = self.chosen_paths.copy()
        for path in chosen_paths:
            if path.is_file():
                os.remove(path)
            else:
                rm_tree(path)
            self.chosen_paths.remove(path)

        self._emit_secondary_selection_changed()
        self.update_source_directory(self.path)
        self.refresh()

    def _emit_secondary_selection_changed(self) -> None:
        self.emit_no_wait(Directory.SecondarySelectionChanged(self, self.chosen_paths))

    def _on_mouse_scroll_down(self, event) -> None:
        if self.has_focus and self.cursor_movement_enabled:
            self.selected_index += 1

    def _on_mouse_scroll_up(self, event) -> None:
        if self.has_focus and self.cursor_movement_enabled:
            self.selected_index -= 1

    def _clamp_index(self, new_index: int) -> int | None:
        """Ensure the selected index stays within range"""
        if not self._files:
            return 0
        return clamp(new_index, 0, len(self._files) - 1)

    def watch_filter(self, new_filter: str):
        self._files = [file for file in list_files_in_dir(self.path) if
                       re.match(new_filter, file.name)]
        self.selected_index = 0 if self._files else None

    @property
    def current_highlighted_path(self):
        if not self._files:
            return None
        return self._files[self.selected_index]

    def update_source_directory(self, new_path: Path | None) -> None:
        if new_path is not None:
            self.path = new_path
            self._files = list_files_in_dir(new_path)
        self.selected_index = 0 if len(self._files) > 0 else None

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return max(len(self._files), container.height)

    def on_focus(self, event: events.Focus) -> None:
        search_input = self.app.query_one("#directory-search-input")
        if search_input.value != "":
            warning_banner = self.app.query_one("#current-dir-filter-warning")
            warning_banner.display = True

    def on_blur(self, event: events.Blur):
        warning_banner = self.app.query_one("#current-dir-filter-warning")
        warning_banner.display = False

    def select_path(self, path: Path):
        if path is None:
            self.selected_index = 0
            return

        try:
            index = self._files.index(path)
        except ValueError:
            index = 0
        self.selected_index = index

    def render(self) -> RenderableType:
        dir_style = self.get_component_rich_style("directory--dir")
        highlight_style = self.get_component_rich_style("directory--highlighted")
        highlight_meta_column_style = self.get_component_rich_style(
            "directory--highlighted-meta-column"
        )
        meta_column_style = self.get_component_rich_style("directory--meta-column")
        highlight_dir_style = self.get_component_rich_style(
            "directory--highlighted-dir"
        )
        chosen_path_style = self.get_component_rich_style("directory--chosen-path")
        chosen_path_meta_style = self.get_component_rich_style("directory--chosen-path-meta")

        chosen_path_selected_style = self.get_component_rich_style("directory--chosen-path-selected")
        chosen_path_selected_meta_style = self.get_component_rich_style("directory--chosen-path-selected-meta")
        return DirectoryListRenderable(
            files=self._files,
            selected_index=self.selected_index,
            filter=self.filter,
            dir_style=dir_style,
            highlight_style=highlight_style,
            highlight_dir_style=highlight_dir_style,
            meta_column_style=meta_column_style,
            highlight_meta_column_style=highlight_meta_column_style,
            chosen_path_style=chosen_path_style,
            chosen_path_meta_style=chosen_path_meta_style,
            chosen_path_selected_style=chosen_path_selected_style,
            chosen_path_selected_meta_style=chosen_path_selected_meta_style,
            chosen_paths=self.chosen_paths,
        )

    class CurrentDirChanged(Message, bubble=True):
        def __init__(
            self, sender: DOMNode, new_dir: Path, from_dir: Path | None
        ) -> None:
            """
            Args:
                sender: The sending node
                new_dir: The new active current dir
                from_dir: Only relevant when we step up the file hierarchy,
                    for ensuring initial selection in parent starts at correct place.
            """
            self.new_dir = new_dir
            self.from_dir = from_dir
            super().__init__(sender)

    class FilePreviewChanged(Message, bubble=True):
        """Should be sent to the app when the selected file is changed."""

        def __init__(self, sender: DOMNode, path: Path) -> None:
            self.path = path
            super().__init__(sender)

    class SecondarySelectionChanged(Message, bubble=True):
        """Should be sent to the app when the secondary selection is changed."""

        def __init__(self, sender: DOMNode, selection: set[Path]) -> None:
            self.sender = sender
            self.selection = selection
            super().__init__(sender)
