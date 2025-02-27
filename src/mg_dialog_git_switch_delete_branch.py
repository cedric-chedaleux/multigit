#     Copyright (c) 2019-2023 IDEMIA
#     Author: IDEMIA (Philippe Fremy, Florent Oulieres)
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#

from typing import TYPE_CHECKING, Any, Tuple, cast, Set, Dict, List, Optional
import logging
from collections import defaultdict
import enum

from PySide6.QtWidgets import QMessageBox, QTreeWidgetItem, QApplication, QProgressDialog, \
                            QTreeWidget, QWidget, QAbstractItemView
from PySide6.QtCore import QTimer, Qt

if TYPE_CHECKING:
    pass
from src.gui.ui_git_switch_branch import Ui_GitSwitchBranch
from src.mg_dialog_utils import MgDialogWithRepoList
from src.mg_exec_window import MgExecWindow
from src.mg_utils import extractInt, istrcmp, treeWidgetDeepIterator
from src.mg_repo_info import MgRepoInfo
from src import mg_config as mgc
from src.mg_ensure_info_available import MgEnsureInfoAvailable, RepoInfoFlags
from src.mg_exec_task_item import MgExecTaskGit, MgExecTaskGroup
from src import mg_const

class GroupingBy(enum.Enum):
    NONE = 0
    NAME = 1

groupingLabel = {
    GroupingBy.NONE: 'No grouping',
    GroupingBy.NAME: 'Group by name',
}

class DeleteOrSwitch(enum.Enum):
    DELETE        = enum.auto()
    SWITCH_BRANCH = enum.auto()
    CHECKOUT_TAG  = enum.auto()


class ItemRole(enum.Enum):
    BRANCH_TAG_MIDDLE_NAME = 0
    BRANCH_TAG_END_NAME  = 1
    ITEM_REPOSITORY  = 2


IDX_SWITCH_BRANCH = 0
IDX_SWITCH_TAG = 1

COL_BRANCH_TAG_NAME = 0
COL_NB_REPO = 1
COL_BRANCH_TYPE = 2

logger = logging.getLogger('mg_dialog_git_switch_delete_branch')
dbg = logger.debug

def stripOrigin(branches_remote: List[str]) -> List[str]:
    '''Strip the first part of a remote url (the name of the remote, usually, 'origin')'''
    return list(sorted(set(name.split('/', 1)[1] for name in branches_remote)))


def branchNameIsPresentInRemote(branchName: str, branches_remote: List[str]) -> bool:
    '''Return whether one of the remote branches contains branch name, when ignoring
    the origin part.

    Example:
        branchNameIsPresentInRemote('toto', ['origin/toto']) -> True
        branchNameIsPresentInRemote('toto', ['origin/titi']) -> False
    '''
    return branchName in set(name.split('/', 1)[1] for name in branches_remote)


def remoteBranchesForBranchName(branchName: str, branches_remote: List[str]) -> List[str]:
    '''Return the list of all the remote branches containing the branch <branchName>

    Example:
        remoteBranchesForBranchName('toto', ['origin/toto']) -> ['origin/toto']
        remoteBranchesForBranchName('titi', ['origin/toto']) -> []
        remoteBranchesForBranchName('toto', ['origin/toto', 'origin2/toto']) -> ['origin/toto', 'origin2/toto']
    '''
    return [name
               for name in branches_remote
               if branchName == name.split('/', 1)[1]]


def buildRepoBranchInfo(targetedRepos: List[MgRepoInfo]) -> List[Tuple[str, List[str], List[str]]]:
    repoBranchInfo = [(repo.name,
                       repo.branches_local,
                       # strip the name of the remote from the remote branches
                       stripOrigin(repo.branches_remote))
                             for repo in targetedRepos]
    return repoBranchInfo


def analyseRepoBranchOrTagInfo(repoBranchInfo: List[Tuple[str, List[str], List[str]]]) \
        -> List[ Tuple[str, int, str, List[str]] ]:
    '''Analyse the branches or tag of all the repositories and return an information suitable
    for display about which branch are present in how many repositories, in local or remote form.

    For tag, it will just present information about how many repositories are present with a given tag. Just
    ignore the third part of the tuple which makes no sense for tags.

    Example: analyseRepoBranchOrTagInfo( [
        ('repo1', ['master', 'local_branch'], ['master', 'remote_branch'])
        ('repo2', ['dev', 'local_branch'], ['dev', 'remote_branch'])
    ])
    => [ ('master', 1, 'local and remote', ['repo1']),
         ('dev', 1, 'local and remote', ['repo2']),
         ('local_branch', 2, 'local', ['repo1', 'repo2']),
         ('remote_branch', 2, 'remote', ['repo1', 'repo2']),
       ]

    Example: analyseRepoBranchOrTagInfo( [ ('repo1', ['tag1', 'tag2'], []), ('repo2', ['tag2', 'tag3'], [])])
    => [ ('tag1', 1, 'local', ['repo1'] ),
         ('tag2', 2, 'local', ['repo1', 'repo2']),
         ('tag3', 1, 'local', ['repo2']),
       ]

    '''
    local_br_dict: Dict[str, Set[str]] = defaultdict(set)
    remote_br_dict: Dict[str, Set[str]] = defaultdict(set)
    local_and_remote_br_dict: Dict[str, Set[str]] = defaultdict(set)

    for repo, local_branches, remote_branches in repoBranchInfo:
        set_local_and_remote_branches = set(local_branches) & set(remote_branches)
        set_only_local_branches = set(local_branches) - set_local_and_remote_branches
        set_only_remote_branches = set(remote_branches) - set_local_and_remote_branches
        assert len(set_only_local_branches) + len(set_local_and_remote_branches) == len(local_branches)
        assert len(set_only_remote_branches) + len(set_local_and_remote_branches) == len(remote_branches)

        for local_br in set_only_local_branches:
            local_br_dict[local_br].add(repo)

        for remote_br in set_only_remote_branches:
            remote_br_dict[remote_br].add(repo)

        for local_and_remote_br in set_local_and_remote_branches:
            local_and_remote_br_dict[local_and_remote_br].add(repo)


    # move local branches appearing in other remote branches to local_and_remote
    for name in local_br_dict.keys():
        if len(remote_br_dict[name]) > 0:
            local_and_remote_br_dict[name].update(local_br_dict[name])
            local_br_dict[name] = set()
            local_and_remote_br_dict[name].update(remote_br_dict[name])
            remote_br_dict[name] = set()
            continue

        if len(local_and_remote_br_dict[name]) > 0:
            local_and_remote_br_dict[name].update(local_br_dict[name])
            local_br_dict[name] = set()


    for name in remote_br_dict.keys():
        if name in local_and_remote_br_dict:
            local_and_remote_br_dict[name].update(remote_br_dict[name])
            remote_br_dict[name] = set()


    ret = []
    for name, repos in local_br_dict.items():
        if len(repos) > 0:
            ret.append((name, len(repos), 'local', list(sorted(repos))))
    for name, repos in remote_br_dict.items():
        if len(repos) > 0:
            ret.append((name, len(repos), 'remote', list(sorted(repos))))
    for name, repos in local_and_remote_br_dict.items():
        if len(repos) > 0:
            ret.append((name, len(repos), 'local and remote', list(sorted(repos))))

    return ret


def applyFilterToTree(tree: QTreeWidget, filterText: str) -> None:
    '''Apply a filter to the tree of repositories with the following rules:
    - matching is case insensitive and can match in the middle of a word
    - branch/tag names are searched and hidden if they don't match
    - list of repositories of a branch/tag are shown if the branch/tag is shown
    '''
    filterText = filterText.lower()

    # let's define some utility functions
    def unhideItemParents(item: QTreeWidgetItem) -> None:
        '''Unhide the item and all parent items'''
        it = item
        while it:
            it.setHidden(False)
            it = it.parent()

    def unhideItemChildren(item: QTreeWidgetItem) -> None:
        '''Unhide the item and all its children'''
        item.setHidden(False)
        for idx in range(item.childCount()):
            unhideItemChildren(item.child(idx))

    def unhideItemHierarchy(item: QTreeWidgetItem) -> None:
        unhideItemParents(item)
        unhideItemChildren(item)


    # Unhide everything
    if not filterText:
        for item in treeWidgetDeepIterator(tree):
            item.setHidden(False)
        return

    # Hide everything
    for item in treeWidgetDeepIterator(tree):
        item.setHidden(True)

    # we wander through all items in DFS
    for item in treeWidgetDeepIterator(tree):
        # only end tag/branch names items are relevant for filtering
        if item.data(0, Qt.ItemDataRole.UserRole) != ItemRole.BRANCH_TAG_END_NAME:
            continue

        data = item.data(0, Qt.ItemDataRole.ToolTipRole)
        if data and filterText in data.lower():
            unhideItemHierarchy(item)
            continue

        # check other columns
        for i in range(1,3):
            if tree.isColumnHidden(i):
                continue
            if filterText in item.text(i).lower():
                unhideItemHierarchy(item)
                continue



class RepoBranchInfoTreeItem(QTreeWidgetItem):

    def __lt__(self, other: 'QTreeWidgetItem') -> bool:
        col = self.treeWidget().sortColumn()
        if col != 1:
            # regular sorting
            return istrcmp(self.text(col), other.text(col))

        colTextSelf = self.text(col)
        colTextOther = other.text(col)
        if len(colTextSelf) and len(colTextOther) and colTextSelf[0].isdigit() and colTextOther[0].isdigit():
            # natural number sorting if we can
            return extractInt(colTextSelf) < extractInt(colTextOther)

        # regular sort strategy will compare strings and place all number starting strings before others
        return istrcmp(self.text(col), other.text(col))


    def findChildByName(self, col: int, name: str) -> 'Optional[RepoBranchInfoTreeItem]':
        '''Search all direct children for an item with the exact name in the given column'''
        for idx in range(self.childCount()):
            if self.child(idx).text(col) == name:
                return cast(RepoBranchInfoTreeItem, self.child(idx))
        return None


    @staticmethod
    def autoAdjustColumnSize(treeWidget: QTreeWidget) -> None:
        '''Adjust automatically the column size to the largest item'''
        for i in range(treeWidget.columnCount()):
            treeWidget.resizeColumnToContents(i)
        QApplication.processEvents()


def fillBranchTagInfo(repoItemInfo: List[Tuple[str, int, str, List[str]]],
                      treeWidget: QTreeWidget,
                      grouping: GroupingBy) -> None:
    '''Fills the treeWidget with items and a nested structure according to grouping
    and repoItemInfo. Works for both tags and branch repoItemInfo

    See analyseRepoBranchOrTagInfo() for the details of the structure.
    '''
    treeWidget.clear()
    if grouping == GroupingBy.NONE:
        for name, count, infoLocalRemote, branchList in repoItemInfo:
            item = RepoBranchInfoTreeItem([name, '%d' % count, infoLocalRemote])
            item.setData(0, Qt.ItemDataRole.ToolTipRole, name)
            item.setData(1, Qt.ItemDataRole.ToolTipRole, 'Present in:\n' + '\n'.join(branchList))
            item.setData(0, Qt.ItemDataRole.UserRole, ItemRole.BRANCH_TAG_END_NAME)
            for branch in branchList:
                childItem = QTreeWidgetItem(['', '    ' + branch])
                childItem.setData(0, Qt.ItemDataRole.UserRole, ItemRole.ITEM_REPOSITORY)
                item.addChild(childItem)
            item.setExpanded(False)
            treeWidget.addTopLevelItem(item)
        treeWidget.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        return

    if grouping == GroupingBy.NAME:
        repoItemInfo.sort()

        def propagateItemContent(parentItem: RepoBranchInfoTreeItem,
                                 splittedName: List[str],
                                 fullName: str,
                                 count: int,
                                 infoLocalRemote: str,
                                 branchList: List[str]) -> None:
            '''Continue to create item to represent the full splitted branch/tag name.

            A parent item already exists, the role of this function is to create one child item for each
            part of splittedName. When the name is exhausted, items with the list of repositories are added.
            '''
            assert len(splittedName)
            # if we don't have and item with our splitted name part in the childs of our parent, create it
            if parentItem.childCount() == 0 or parentItem.findChildByName(0, splittedName[0]) is None:
                childItem: QTreeWidgetItem = RepoBranchInfoTreeItem([splittedName[0]])
                # Note that with the information available so far, we set it as middle-name. But if it turns
                # out that the name is the final part, this will be overridden with BRANCH_TAG_END_NAME
                childItem.setData(0, Qt.ItemDataRole.UserRole, ItemRole.BRANCH_TAG_MIDDLE_NAME)
                parentItem.addChild(childItem)
                parentItem.setExpanded(True)

            item = parentItem.findChildByName(0, splittedName[0])
            assert item is not None
            if len(splittedName) == 1:
                # we just filled the end of the item name, also fill other columns
                item.setText(1, str(count))
                item.setText(2, infoLocalRemote)
                item.setData(0, Qt.ItemDataRole.ToolTipRole, fullName)
                item.setData(1, Qt.ItemDataRole.ToolTipRole, 'Present in:\n' + '\n'.join(branchList))
                item.setData(0, Qt.ItemDataRole.UserRole, ItemRole.BRANCH_TAG_END_NAME)
                for branch in branchList:
                    childItem = QTreeWidgetItem(['', '    ' + branch])
                    childItem.setData(0, Qt.ItemDataRole.UserRole, ItemRole.ITEM_REPOSITORY)
                    item.addChild(childItem)
                item.setExpanded(False)
            else:
                # more items to fill
                propagateItemContent(item, splittedName[1:], fullName, count, infoLocalRemote, branchList)


        lastTopLevel: Optional[RepoBranchInfoTreeItem] = None
        for name, count, infoLocalRemote, branchList in repoItemInfo:
            splittedName = name.split('/')
            if lastTopLevel is None or splittedName[0] != lastTopLevel.text(0):
                # create a new top-level item
                item = RepoBranchInfoTreeItem(treeWidget, [splittedName[0]])
            else:
                item = lastTopLevel
            if len(splittedName) > 1:
                item.setData(0, Qt.ItemDataRole.UserRole, ItemRole.BRANCH_TAG_MIDDLE_NAME)
                propagateItemContent(item, splittedName[1:], name, count, infoLocalRemote, branchList)
            else:
                item.setText(1, str(count))
                item.setText(2, infoLocalRemote)
                item.setData(0, Qt.ItemDataRole.ToolTipRole, name)
                item.setData(1, Qt.ItemDataRole.ToolTipRole, 'Present in:\n' + '\n'.join(branchList))
                item.setData(0, Qt.ItemDataRole.UserRole, ItemRole.BRANCH_TAG_END_NAME)
                for branch in branchList:
                    childItem = QTreeWidgetItem(['', '    ' + branch])
                    childItem.setData(0, Qt.ItemDataRole.UserRole, ItemRole.ITEM_REPOSITORY)
                    item.addChild(childItem)
                item.setExpanded(False)

            lastTopLevel = item

    return


class MgDialogGitSwitchDeleteBranch(MgDialogWithRepoList):
    ui: Ui_GitSwitchBranch
    progressDialog: QProgressDialog

    def __init__(self, parent: QWidget,
                 deleteOrSwitch: DeleteOrSwitch,
                 selectedRepos:
                 List[MgRepoInfo],
                 allRepos: List[MgRepoInfo]
                 ) -> None:
        super().__init__(parent, Ui_GitSwitchBranch, selectedRepos, allRepos)

        self.repoNamesMissingBranchOrTagInfo: Set[str] = set()
        self.deleteOrSwitch = deleteOrSwitch

        self.ui.pushButtonGrouping.clicked.connect(self.slotChangeGrouping)
        self.sigRepoListAdjusted.connect(self.ensureBranchTagInfoAvailable)

        self.ui.pushButtonGrouping.setFixedSize(self.ui.pushButtonGrouping.sizeHint())
        self.grouping = GroupingBy.NONE
        self.slotChangeGrouping()

        self.ui.treeWidgetBranches.clear()
        self.ui.treeWidgetBranches.setSortingEnabled(True)
        self.ui.treeWidgetBranches.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.ui.treeWidgetBranches.itemSelectionChanged.connect(self.slotItemSelectionChanged)

        self.ui.lineEditBranchFilter.setPlaceholderText('Filter the list of %s by typing here' % ('branches' if self.isBranchDialog() else 'tags'))
        self.ui.lineEditBranchFilter.setClearButtonEnabled(True)
        self.ui.lineEditBranchFilter.textEdited.connect(self.slotApplyFilter)
        self.ui.lineEditBranchTagName.setPlaceholderText('Choose %s from list below or type it here' % ('branch' if self.isBranchDialog() else 'tag'))

        if deleteOrSwitch == DeleteOrSwitch.DELETE:
            self.setWindowTitle('Git Delete Branch')
            self.ui.labelBranchOrTag.setText('Git delete branch')
            self.ui.labelBranchOrTag.setVisible(True)
            self.ui.checkBoxDefaultForNotExist.setVisible(False)
            self.ui.checkBoxDeleteLocalBranch.setVisible(True)
            self.ui.checkBoxDeleteRemoteBranch.setVisible(True)
            self.ui.checkBoxDefaultForNotExist.setVisible(False)
            self.ui.treeWidgetBranches.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self.ui.treeWidgetBranches.headerItem().setText(0, 'Branches')
        elif deleteOrSwitch == DeleteOrSwitch.SWITCH_BRANCH:
            self.setWindowTitle('Git Switch Branch')
            self.ui.labelBranchOrTag.setText('Choose branch')
            self.ui.groupBoxBranchOrTagSelection.setTitle('Branch selection')
            self.ui.labelBranchOrTag.setVisible(True)
            self.ui.checkBoxDeleteRemoteBranch.setVisible(False)
            self.ui.checkBoxDeleteLocalBranch.setVisible(False)
            self.ui.checkBoxDefaultForNotExist.setVisible(True)
            self.ui.treeWidgetBranches.setColumnHidden(COL_BRANCH_TYPE, False)
            self.ui.treeWidgetBranches.headerItem().setText(0, 'Branches')
        else:
            assert deleteOrSwitch == DeleteOrSwitch.CHECKOUT_TAG
            self.setWindowTitle('Git Checkout Tag')
            self.ui.labelBranchOrTag.setText('Choose tag')
            self.ui.groupBoxBranchOrTagSelection.setTitle('Tag selection')
            self.ui.labelBranchOrTag.setVisible(True)
            self.ui.checkBoxDeleteRemoteBranch.setVisible(False)
            self.ui.checkBoxDeleteLocalBranch.setVisible(False)
            self.ui.checkBoxDefaultForNotExist.setVisible(False)
            self.ui.treeWidgetBranches.setColumnHidden(COL_BRANCH_TYPE, True)
            self.ui.treeWidgetBranches.headerItem().setText(0, 'Tags')


    def exec_(self) -> Any:
        QTimer.singleShot(0, self.ensureBranchTagInfoAvailable)
        return super().exec_()


    def slotItemSelectionChanged(self) -> None:
        '''The selection has changed. In delete mode, multiple items may be selected.
        In switch mode, only one branch/tag may be selected so this will contain only one item'''
        items = self.ui.treeWidgetBranches.selectedItems()
        if len(items) == 0:
            # nothing selected, difficult to believe
            return

        branchNames = [self.resolveBranchName(item) for item in items]
        branchNamesExcludingNone = [name for name in branchNames if name]
        self.ui.lineEditBranchTagName.setText( '  '.join(branchNamesExcludingNone) )


    def resolveBranchName(self, item: QTreeWidgetItem) -> str:
        '''Return the full branch name, when branches are grouped in a hierarchy:

        + feat/                 -> if selected, returns ''
            + new_feat_1        -> if selected, returns feat/new_feat_1
            + new_feat_2        -> if selected, returns feat/new_feat_2

        '''
        if item.data(0, Qt.ItemDataRole.UserRole) == ItemRole.ITEM_REPOSITORY:
            # when pointing to a repository, the branch/tag name ends in his parent
            item = item.parent()

        if item.data(0, Qt.ItemDataRole.UserRole) == ItemRole.BRANCH_TAG_MIDDLE_NAME:
            # for the middle-name, we don't want to report any text
            return ''

        assert item.data(0, Qt.ItemDataRole.UserRole) == ItemRole.BRANCH_TAG_END_NAME

        # item has parent. Collect the name of all parents + name of the item to form
        # the full branch/tag name.
        visitItem = item
        fullBranchTagName = visitItem.text(0)
        while visitItem.parent() is not None:
            visitItem = visitItem.parent()
            fullBranchTagName = '%s/%s' % (visitItem.text(0), fullBranchTagName)
        return fullBranchTagName


    def ensureBranchTagInfoAvailable(self) -> None:
        '''Collect all branches or tags from all selected repositories. If some repositories
        do not have the branch or tag information set yet, trigger the information reading on the repository.
        If the operation takes too long, show a progress dialog
        '''
        QApplication.processEvents()
        targetedRepos = self.getTargetedRepoList()
        self.ensureInfoAvailable = MgEnsureInfoAvailable(self, targetedRepos, showProgressDialog=True)

        if self.deleteOrSwitch in (DeleteOrSwitch.SWITCH_BRANCH, DeleteOrSwitch.DELETE):
            repoInfo = RepoInfoFlags.ALL_BRANCHES
        else:
            repoInfo = RepoInfoFlags.ALL_TAGS

        self.ensureInfoAvailable.ensureInfoAvailable(repoInfo, blocking=True)
        self.fillTreeWidgetBranchTagSelection()


    def slotChangeGrouping(self) -> None:
        '''Change the grouping option used to display all the branches'''
        try:
            self.grouping = GroupingBy(self.grouping.value+1)
        except ValueError:
            self.grouping = GroupingBy.NONE
        try:
            nextGrouping = GroupingBy(self.grouping.value+1)
        except ValueError:
            nextGrouping = GroupingBy.NONE

        self.ui.pushButtonGrouping.setText(groupingLabel[nextGrouping])
        self.fillTreeWidgetBranchTagSelection()


    def fillTreeWidgetBranchTagSelection(self) -> None:
        '''Fills the tree widget dedicated to displaying all possible branches'''
        targetedRepos = self.getTargetedRepoList()

        repoItemInfo: List[ Tuple[str, int, str, List[str]] ]
        repoBranchTagInfo: List[ Tuple[str, List[str], List[str]]]
        if self.isBranchDialog():
            repoBranchTagInfo = buildRepoBranchInfo(targetedRepos)
        else:
            repoBranchTagInfo = [(repo.name, repo.all_tags, [])
                           for repo in targetedRepos]

        repoItemInfo = analyseRepoBranchOrTagInfo(repoBranchTagInfo)
        self.ui.treeWidgetBranches.clear()
        fillBranchTagInfo(repoItemInfo, self.ui.treeWidgetBranches, self.grouping)
        self.ui.treeWidgetBranches.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        RepoBranchInfoTreeItem.autoAdjustColumnSize(self.ui.treeWidgetBranches)
        self.slotApplyFilter()



    def slotApplyFilter(self) -> None:
        '''Called when the user modifies text of the branch line edit. Trigger filtering
        the content of the items'''
        filterText = self.ui.lineEditBranchFilter.text().lower()
        applyFilterToTree(self.ui.treeWidgetBranches, filterText)

    def getTargetedBranchTag(self) -> str:
        '''Get the branch selected in either local or remote tree'''
        if self.deleteOrSwitch == DeleteOrSwitch.DELETE:
            raise ValueError('Do not use this method in DELETE branch mode')
        return self.ui.lineEditBranchTagName.text()


    def getDeleteTargetedBranches(self) -> List[str]:
        '''Get the branch selected in either local or remote tree'''
        if self.deleteOrSwitch != DeleteOrSwitch.DELETE:
            raise ValueError('Do not use this method when not in DELETE branch mode')
        branchesText =  self.ui.lineEditBranchTagName.text()
        branchesWithEmpty = branchesText.split(' ')
        branches = [br for br in branchesWithEmpty if br]
        return branches


    def isBranchDialog(self) -> bool:
        '''Return True if dialog is about branch (switching, deleting)'''
        return self.deleteOrSwitch in (DeleteOrSwitch.SWITCH_BRANCH, DeleteOrSwitch.DELETE)


    def checkAcceptDeleteBranch(self, targetBranch: str) -> bool:
        '''Check if delete branch is consistent and return True if this is the case.

        If inconsistent, asks the user what he wants to do and return True/False depending on his answer'''
        repoBranchInfo = buildRepoBranchInfo(self.getTargetedRepoList())

        if not self.ui.checkBoxDeleteRemoteBranch.isChecked() and not self.ui.checkBoxDeleteLocalBranch.isChecked():
            msg = f'You must check at least one of  <i>Delete local branch</i> and <i>Delete remote branch</i>.'
            msg += '<p>What do you want to do ?<p>'
            msgBox = QMessageBox(self)
            msgBox.setWindowTitle('Must choose local or remote branch to delete ?')
            msgBox.setTextFormat(Qt.TextFormat.RichText)
            msgBox.setText(msg)

            delLocalButton = msgBox.addButton('Delete local branch', QMessageBox.ButtonRole.AcceptRole)
            delRemoteButton = msgBox.addButton('Delete remote branch', QMessageBox.ButtonRole.AcceptRole)
            delBothButton = msgBox.addButton('Delete local and remote branch', QMessageBox.ButtonRole.AcceptRole)
            cancelButton = msgBox.addButton(QMessageBox.StandardButton.Cancel)
            msgBox.setDefaultButton(cancelButton)
            msgBox.exec()
            buttonSelected = msgBox.clickedButton()

            if buttonSelected == cancelButton:
                # user changed its mind ...
                return False

            if buttonSelected in (delLocalButton, delBothButton):
                self.ui.checkBoxDeleteLocalBranch.setChecked(True)

            if buttonSelected in (delRemoteButton, delBothButton):
                self.ui.checkBoxDeleteRemoteBranch.setChecked(True)


        localRepoWithTargetedBranch = [repo
                                       for repo, repoLocalBranches, repoRemoteBranches in repoBranchInfo
                                       if targetBranch in repoLocalBranches
                                       ]

        remoteRepoWithTargetedBranch = [repo
                                        for repo, repoLocalBranches, repoRemoteBranches in repoBranchInfo
                                        if targetBranch in repoRemoteBranches
                                        ]

        if len(localRepoWithTargetedBranch) + len(remoteRepoWithTargetedBranch) == 0:
            # this branch name does not exist in any repos!
            QMessageBox.warning(self, "Invalid branch name",
                                'No repository exists with the branch: %s' % targetBranch)
            return False

        # branch exists only remotely but remote check box is not checked
        if len(localRepoWithTargetedBranch) == 0 and len(remoteRepoWithTargetedBranch) > 0 \
                and not self.ui.checkBoxDeleteRemoteBranch.isChecked():
            msg = f'You want to delete branch {targetBranch}, which exists only remotely in the repositories '
            msg += 'but you did not check the <i>Delete remote branch</i>'
            msg += '<p>What do you want to do ?<p>'
            msgBox = QMessageBox(self)
            msgBox.setWindowTitle('Delete remote branch ?')
            msgBox.setTextFormat(Qt.TextFormat.RichText)
            msgBox.setText(msg)

            continueButton = msgBox.addButton('Continue', QMessageBox.ButtonRole.AcceptRole)
            delButton = msgBox.addButton('Delete remote branch', QMessageBox.ButtonRole.AcceptRole)
            cancelButton = msgBox.addButton(QMessageBox.StandardButton.Cancel)
            msgBox.setDefaultButton(delButton)
            msgBox.exec()
            buttonSelected = msgBox.clickedButton()

            if buttonSelected == cancelButton:
                # user changed its mind ...
                return False

            if buttonSelected == delButton:
                self.ui.checkBoxDeleteRemoteBranch.setChecked(True)

        # branch exists only locally but local check box is not checked
        if len(localRepoWithTargetedBranch) > 0 and len(remoteRepoWithTargetedBranch) == 0 \
                and not self.ui.checkBoxDeleteLocalBranch.isChecked():
            msg = f'You want to delete branch {targetBranch}, which exists only locally in the repositories '
            msg += 'but you did not check the <i>Delete local branch</i>'
            msg += '<p>What do you want to do ?<p>'
            msgBox = QMessageBox(self)
            msgBox.setWindowTitle('Delete local branch ?')
            msgBox.setTextFormat(Qt.TextFormat.RichText)
            msgBox.setText(msg)

            continueButton = msgBox.addButton('Continue', QMessageBox.ButtonRole.AcceptRole)
            delButton = msgBox.addButton('Delete local branch', QMessageBox.ButtonRole.AcceptRole)
            cancelButton = msgBox.addButton(QMessageBox.StandardButton.Cancel)
            msgBox.setDefaultButton(delButton)
            msgBox.exec()
            buttonSelected = msgBox.clickedButton()

            if buttonSelected == cancelButton:
                # user changed its mind ...
                return False

            if buttonSelected == delButton:
                self.ui.checkBoxDeleteLocalBranch.setChecked(True)

        mgc.get_config_instance().lruSetRecent(mgc.CONFIG_GIT_BRANCH_HISTORY, targetBranch)
        return True


    def checkAcceptSwitchBranch(self) -> bool:
        '''Check if switching to branch or tag is coherent'''
        repoBranchInfo = buildRepoBranchInfo(self.getTargetedRepoList())

        repoWithTargetedBranch = [repo
                                  for repo, repoLocalBranches, repoRemoteBranches in repoBranchInfo
                                  if self.getTargetedBranchTag() in repoLocalBranches
                                  or self.getTargetedBranchTag() in repoRemoteBranches
                                  ]

        if len(repoWithTargetedBranch) == 0:
            # this branch name does not exist in any repos!
            QMessageBox.warning(self, "Invalid branch name",
                                'No repository exists with the branch: %s' % self.getTargetedBranchTag())
            return False

        mgc.get_config_instance().lruSetRecent(mgc.CONFIG_GIT_BRANCH_HISTORY, self.getTargetedBranchTag())
        return True


    def checkAcceptCheckoutTag(self) -> bool:
        # switch to tag
        all_repo_tags: Set[str] = set()
        for repo in self.getTargetedRepoList():
            all_repo_tags.update(repo.all_tags)

        if not self.getTargetedBranchTag() in all_repo_tags:
            QMessageBox.warning(self, "Invalid tag name",
                                'No repository exists with the tag: %s' % self.getTargetedBranchTag())
            return False

        mgc.get_config_instance().lruSetRecent(mgc.CONFIG_TAG_HISTORY, self.getTargetedBranchTag())
        return True


    def accept(self) -> None:
        if ((self.deleteOrSwitch in (DeleteOrSwitch.SWITCH_BRANCH, DeleteOrSwitch.CHECKOUT_TAG) and self.getTargetedBranchTag() == '')
            or (self.deleteOrSwitch == DeleteOrSwitch.DELETE and self.getDeleteTargetedBranches() == [])):
            # no branch/tag is actually selected!
            branchOrTag = 'branch' if self.isBranchDialog() else 'tag'
            QMessageBox.warning(self, "No %s selected" % branchOrTag,
                                "You did not select a %s to checkout!" % branchOrTag)
            return

        if self.deleteOrSwitch == DeleteOrSwitch.DELETE:
            for targetBranch in self.getDeleteTargetedBranches():
                if not self.checkAcceptDeleteBranch(targetBranch):
                    return

        elif self.deleteOrSwitch == DeleteOrSwitch.SWITCH_BRANCH:
            if not self.checkAcceptSwitchBranch():
                return

        elif self.deleteOrSwitch == DeleteOrSwitch.CHECKOUT_TAG:
            if not self.checkAcceptCheckoutTag():
                return

        else:
            raise ValueError('Should not be reached!')

        mgc.get_config_instance().save()
        super().accept()


def runDialogGitSwitchDelete(parent: QWidget, deleteOrSwitch: DeleteOrSwitch, selectedRepos: List[MgRepoInfo], allRepos: List[MgRepoInfo]) -> None:
    '''Run a dialog to switch or delete a branch'''
    dbg('runDialogGitSwitchDelete')

    dialog = MgDialogGitSwitchDeleteBranch(parent, deleteOrSwitch, selectedRepos, allRepos)
    result = dialog.exec_()
    if not result:
        # command execution canceled
        return

    if deleteOrSwitch == DeleteOrSwitch.DELETE:
        doGitDeleteBranch(parent, dialog)
    elif deleteOrSwitch in (DeleteOrSwitch.SWITCH_BRANCH, DeleteOrSwitch.CHECKOUT_TAG):
        if not doGitSwitchBranchTag(parent, dialog):
            # re-run the dialog
            runDialogGitSwitchDelete(parent, deleteOrSwitch, selectedRepos, allRepos)
    else:
        raise ValueError('No such value: %s' % deleteOrSwitch)


def doGitDeleteBranch(parent: QWidget, dialog: MgDialogGitSwitchDeleteBranch) -> None:

    # use the same history as git create branch
    descGitDelete = 'Git Delete branch'

    descRepoCmdBranch = []
    deleteBranchesName = dialog.getDeleteTargetedBranches()
    for repo in dialog.getTargetedRepoList():
        gitCmds = []

        for branchName in deleteBranchesName:

            gitDeleteRemoteBranch = ['push', 'origin', '--delete', branchName]
            gitDeleteLocalBranch = ['branch', '-d', branchName]

            ### Delete remote first, so that we can delete local branch even if not fully merged
            #   into remote.
            if branchName in [name[name.index('/') + 1:] for name in
                                 repo.branches_remote] and dialog.ui.checkBoxDeleteRemoteBranch.isChecked():
                # delete remotely
                gitCmds.append(gitDeleteRemoteBranch)

            if branchName in repo.branches_local and dialog.ui.checkBoxDeleteLocalBranch.isChecked():
                # delete locally
                gitCmds.append(gitDeleteLocalBranch)

        if len(gitCmds) > 0:
            descRepoCmdBranch.append((descGitDelete, repo, gitCmds))

    # show window for executing git
    if len(descRepoCmdBranch):
        gitExecWindow = MgExecWindow(parent)
        gitExecWindow.execEachRepoWithHisSeqOfGitCommand(descGitDelete, descRepoCmdBranch)
    else:
        QMessageBox.warning(parent, 'No repository',
                            'No repository found for branch names: {}.'.format(', '.join(deleteBranchesName)))


def doGitSwitchBranchTag(parent: QWidget, dialog: MgDialogGitSwitchDeleteBranch) -> bool:
    # switch branch/tag
    branchTagName = dialog.getTargetedBranchTag()

    gitCheckoutBranch = [['checkout', branchTagName, '--']]
    gitCheckoutBranchInt = [['checkout', 'int', '--']]
    descCheckout = 'Checkouting %s %s ' % ('branch' if dialog.isBranchDialog() else 'tag', branchTagName)
    descCheckoutInt = 'Checkouting backup branch int'
    taskGroupsCmdBranchTag = []
    taskGroupsCmdInt = []

    if dialog.isBranchDialog():
        # checkouting a branch
        for repo in dialog.getTargetedRepoList():
            if branchTagName in repo.branches_local:
                # local branch found
                taskGroupsCmdBranchTag.append(
                    MgExecTaskGroup(descCheckout, repo,
                                                        [MgExecTaskGit(descCheckout, repo, cmdLine)
                                                         for cmdLine in gitCheckoutBranch]))

            elif branchNameIsPresentInRemote(branchTagName, repo.branches_remote):
                # remote branch found!

                # present in multiple origins ?
                remoteBranches = remoteBranchesForBranchName(branchTagName, repo.branches_remote)
                if len(remoteBranches) == 1:
                    # ok, only one origin, simple command:
                    taskGroupsCmdBranchTag.append(
                        MgExecTaskGroup(descCheckout, repo,
                                        [MgExecTaskGit(descCheckout, repo, cmdLine)
                                         for cmdLine in gitCheckoutBranch]))

                else:
                    # multiple origins, ask the user
                    msg1 = f'Multiple remote branches match the name "{branchTagName}" in repository "{repo.name}"\n'
                    msg1 += 'Please select the one you want.'
                    msgBox = QMessageBox(parent)
                    msgBox.setText(msg1)
                    msgBox.setIcon(QMessageBox.Icon.Warning)
                    msgBox.setWindowTitle('Multiple remote branch with same name')
                    buttons = []
                    for branch in remoteBranches:
                        buttons.append(msgBox.addButton(branch, QMessageBox.ButtonRole.ActionRole))
                    msgBox.setStandardButtons(QMessageBox.StandardButton.Abort)

                    stdButtonClicked = msgBox.exec_()
                    if stdButtonClicked == QMessageBox.StandardButton.Abort:
                        # show the dialog again
                        return False

                    clickedButton = msgBox.clickedButton()
                    remoteBranch = clickedButton.text()
                    taskGroupsCmdBranchTag.append(
                        MgExecTaskGroup(f'Checkouting branch {remoteBranch}', repo,
                                        [MgExecTaskGit(f'Checkouting branch {remoteBranch}', repo,
                                                ['checkout', '--track', remoteBranch, '--'] )]
                                        )
                    )


            elif dialog.ui.checkBoxDefaultForNotExist.isChecked():
                # branch not found and default branch specified
                taskGroupsCmdInt.append(
                    MgExecTaskGroup(descCheckoutInt, repo,
                                    [MgExecTaskGit(descCheckoutInt, repo, cmdLine)
                                     for cmdLine in gitCheckoutBranchInt]))

            # else:
            #    do Nothing
    else:
        # checkouting a tag
        # TODO: when tag does not exist and we know it in advance, display a message instead of trying to checkout it
        for repo in dialog.getTargetedRepoList():
            taskGroupsCmdBranchTag.append(
                MgExecTaskGroup(descCheckout, repo, [
                    # we force git to checkout another version first, so that it reflects the tag name in git status
                    MgExecTaskGit(f'{descCheckout} (step 1)', repo, ['checkout', f'{branchTagName}~1', '--'], ignore_failure=True),
                    MgExecTaskGit(f'{descCheckout} (step 2)', repo, ['checkout', branchTagName, '--']),
                ]))

    # show window for executing git
    gitExecWindow = MgExecWindow(parent)
    if len(taskGroupsCmdBranchTag):
        gitExecWindow.execTaskGroups(descCheckout, taskGroupsCmdBranchTag)
    if len(taskGroupsCmdInt):
        gitExecWindow.execTaskGroups(descCheckoutInt, taskGroupsCmdInt)


    return True

# TODO: group branch by number of repositories
# TODO: show names of the repositories when grouping
