class Handler:
    def __init__(self, parent = None):
        self.parent = parent
    def Handle(self, event):
        handler = 'Handle_' +event
        if hasattr(self, handler):
            func = getattr(self, handler)
            func()
        elif self.parent:
            self.parent.Handle(event)

class Geo():
    def __init__(self, h):
        self.handler = h

    def Handle(self, event):
        func = getattr(self.handler, 'Handle')
        func(event)

class Steve():
    def __init__(self, h):
        self.handler = h

    def Handle(self, event):
        func = getattr(self.handler, 'Handle')
        func(event)

class Andy():
    def Handle(self, event):
        print ('Andy is handling %s' %(event))

if __name__ == '__main__':
    a = Andy()
    s = Steve(a)
    g = Geo(s)
    g.Handle('lab on fire')