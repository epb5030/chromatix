"""Project -> Sample Set -> Injection tree."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem

from chromatix_store import Store

Key = tuple[str, int]  # ("project" | "sample_set" | "injection", id)
_KEY_ROLE = Qt.ItemDataRole.UserRole


class ProjectTree(QTreeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setHeaderLabels(["Projects", "Type"])
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setMinimumWidth(260)

    def populate(self, store: Store | None) -> None:
        """Rebuild from the store, keeping the current selection and expansion."""
        selected = self.selected_key()
        expanded = {item.data(0, _KEY_ROLE) for item in self._items() if item.isExpanded()}
        self.clear()
        if store is None:
            return
        for project in store.projects():
            p_item = self._add(self, project.name, "", ("project", project.id))
            for sample_set in store.sample_sets(project.id):
                s_item = self._add(
                    p_item, sample_set.name, "sample set", ("sample_set", sample_set.id)
                )
                for inj in store.injections(sample_set.id):
                    self._add(
                        s_item,
                        f"{inj.position}. {inj.sample_name}",
                        inj.sample_type,
                        ("injection", inj.id),
                    )
        for item in self._items():
            key = item.data(0, _KEY_ROLE)
            item.setExpanded(key in expanded)
            if key == selected:
                self.setCurrentItem(item)

    def _add(
        self, parent: QTreeWidget | QTreeWidgetItem, label: str, kind: str, key: Key
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, kind])
        item.setData(0, _KEY_ROLE, key)
        if isinstance(parent, QTreeWidget):
            parent.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _items(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            out.append(item)
            stack.extend(item.child(i) for i in range(item.childCount()))
        return out

    def selected_key(self) -> Key | None:
        item = self.currentItem()
        return None if item is None else item.data(0, _KEY_ROLE)

    def select(self, key: Key) -> None:
        for item in self._items():
            if item.data(0, _KEY_ROLE) == key:
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                self.setCurrentItem(item)
                return

    def context(self) -> dict[str, int]:
        """IDs of the selected item and its ancestors, e.g. {"project": 1, "sample_set": 3}."""
        found: dict[str, int] = {}
        item = self.currentItem()
        while item is not None:
            kind, ident = item.data(0, _KEY_ROLE)
            found[kind] = ident
            item = item.parent()
        return found
