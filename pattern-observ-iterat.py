from collections.abc import Iterable, Iterator
from abc import ABC, abstractmethod

class Tree:
    def __init__(self, year, event):
        self.year = year
        self.event = event
        self.left_child: Tree = None
        self.right_child: Tree = None
        self.nodes_sorted = []
        self.counter = -1

    def add_child(self, year, event):
        if year < self.year:
            if self.left_child:
                self.left_child.add_child(year, event)
            else:
                self.left_child = Tree(year, event)
        elif year > self.year:
            if self.right_child:
                self.right_child.add_child(year, event)
            else:
                self.right_child = Tree(year, event)
        else:
            pass


class TreeIterator(Iterator):
    def __init__(self, nodes_sorted):
        self._counter = -1
        self._nodes_sorted = nodes_sorted

    def __next__(self):
        if self._counter + 1 < len(self._nodes_sorted):
            self._counter += 1
            return self._nodes_sorted[self._counter]
        else:
            raise StopIteration


class DeepCrawl(object):
    def __init__(self, tree: Tree):
        self._tree = tree
        self._nodes_sorted = []
        self._inorder(self._nodes_sorted, self._tree)

    def __iter__(self):
        return TreeIterator(self._nodes_sorted)

    def _inorder(self, nodes_sorted, node):
        if node.left_child:
            self._inorder(nodes_sorted, node.left_child)
        self._nodes_sorted.append(str(node.year) + ' - ' + node.event)
        if node.right_child:
            self._inorder(nodes_sorted, node.right_child)
class Subject(ABC):
    @abstractmethod
    def attach(self, observer):
        pass

    @abstractmethod
    def detach(self):
        pass

    @abstractmethod
    def notify(self):
        pass


class TreeNotifier(Subject):
    _observers = []

    def attach(self, observer):
        self._observers.append(observer)

    def detach(self, observer):
        try:
            self._observers.remove(observer)
        except ValueError:
            pass

    def notify(self):
        return self._observers

    def createTree(self):
        tree = Tree(123, 'abcda')
        tree.add_child(36, 'abcde')
        tree.add_child(332, 'klmn')
        tree.add_child(784, 'ffrf')
        tree.add_child(887, 'lol')
        tree.add_child(333, 'pertk')
        tree.add_child(555, 'abcd')
        deep = DeepCrawl(tree)
        events = []
        for event in deep:
            events.append(event)
        answer = {'events': events,
                  'clients': self.notify()}
        return answer