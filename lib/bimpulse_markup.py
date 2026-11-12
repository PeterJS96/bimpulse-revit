# -*- coding: utf-8 -*-
"""Draw on a picture of the Revit view before it is sent with a comment: pen, arrow and box,
in red, amber or blue, with undo. The drawing is merged into the picture at its full size.

markup(png_bytes, resources, brush) shows the window and returns the PNG to send (with the
drawing), or None to send the comment without the view.
"""
import math

import clr
clr.AddReference("PresentationCore")
clr.AddReference("PresentationFramework")
clr.AddReference("WindowsBase")
import System
from System.IO import MemoryStream
from System.Windows import (CornerRadius, HorizontalAlignment, Point, Rect, Thickness, VerticalAlignment,
                            Window, WindowStartupLocation, SystemParameters)
from System.Windows.Controls import (Border, Button, Canvas, DockPanel, Dock, Grid, Image, Orientation, StackPanel,
                                     TextBlock, Viewbox)
from System.Windows.Input import Cursors, Key, Keyboard, ModifierKeys
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Media import (BrushConverter, BrushMappingMode, DrawingVisual, PenLineCap, PenLineJoin,
                                  PixelFormats, PointCollection, Stretch, VisualBrush)
from System.Windows.Media.Imaging import BitmapCacheOption, BitmapImage, PngBitmapEncoder, BitmapFrame, \
    RenderTargetBitmap
from System.Windows.Shapes import Line, Polyline, Rectangle

COLOURS = (("Red", "#EF4444"), ("Amber", "#F59E0B"), ("Blue", "#00A0E3"))
TOOLS = (("Pen", "pen"), ("Arrow", "arrow"), ("Box", "box"))


def _bitmap(data):
    img = BitmapImage()
    img.BeginInit()
    img.StreamSource = MemoryStream(System.Array[System.Byte](bytearray(data)))
    img.CacheOption = BitmapCacheOption.OnLoad
    img.EndInit()
    img.Freeze()
    return img


def arrow_head(x1, y1, x2, y2, size):
    """The two short lines of an arrow's head at (x2, y2), as three points."""
    angle = math.atan2(y2 - y1, x2 - x1)
    a, b = angle + math.radians(150), angle - math.radians(150)
    return [(x2 + size * math.cos(a), y2 + size * math.sin(a)), (x2, y2),
            (x2 + size * math.cos(b), y2 + size * math.sin(b))]


class MarkupWindow(Window):
    def __init__(self, png, resources, brush):
        self.brush = brush
        self.result = None
        self.tool, self.colour = "arrow", COLOURS[0][1]
        self.shapes = []  # what was drawn, for undo: each a list of canvas children
        self.drawing = None
        self.picture = _bitmap(png)
        self.w, self.h = self.picture.PixelWidth, self.picture.PixelHeight
        self.thickness = max(3.0, self.w / 260.0)

        self.Title = "BIMPulse: draw on the view"
        if resources is not None:
            self.Resources.MergedDictionaries.Add(resources)
        self.Background = brush("#050B14")
        self.Foreground = brush("#E6EEF7")
        self.FontFamily = System.Windows.Media.FontFamily("Segoe UI")
        area = SystemParameters.WorkArea
        self.Width, self.Height = min(1200, area.Width - 60), min(860, area.Height - 60)
        self.WindowStartupLocation = WindowStartupLocation.CenterScreen
        try:
            WindowInteropHelper(self).Owner = System.Diagnostics.Process.GetCurrentProcess().MainWindowHandle
        except Exception:
            pass

        root = DockPanel()
        root.Margin = Thickness(16, 12, 16, 12)
        root.Children.Add(self._toolbar())
        root.Children.Add(self._footer())
        frame = Border()
        frame.CornerRadius = CornerRadius(10)
        frame.BorderBrush = brush("#1C2B40")
        frame.BorderThickness = Thickness(1)
        frame.Background = brush("#081221")
        frame.Padding = Thickness(6)
        frame.ClipToBounds = True
        sheet = Grid()
        sheet.Width, sheet.Height = self.w, self.h
        image = Image()
        image.Source = self.picture
        image.Stretch = Stretch.Fill
        sheet.Children.Add(image)
        self.canvas = Canvas()
        self.canvas.Width, self.canvas.Height = self.w, self.h
        self.canvas.Background = brush("#00000000")
        self.canvas.Cursor = Cursors.Cross
        self.canvas.ClipToBounds = True
        self.canvas.MouseLeftButtonDown += self.on_down
        self.canvas.MouseMove += self.on_move
        self.canvas.MouseLeftButtonUp += self.on_up
        sheet.Children.Add(self.canvas)
        fit = Viewbox()
        fit.Stretch = Stretch.Uniform
        fit.Child = sheet
        frame.Child = fit
        root.Children.Add(frame)
        self.Content = root
        self.PreviewKeyDown += self.on_key
        self._refresh_buttons()

    # ---------- layout ----------

    def _button(self, label, action, tip=None):
        b = Button()
        b.Content = label
        b.Margin = Thickness(0, 0, 6, 0)
        b.Padding = Thickness(12, 5, 12, 5)
        if tip:
            b.ToolTip = tip
        b.Click += lambda s, a: action()
        return b

    def _toolbar(self):
        bar = StackPanel()
        bar.Orientation = Orientation.Horizontal
        bar.Margin = Thickness(0, 0, 0, 10)
        DockPanel.SetDock(bar, Dock.Top)
        self.tool_buttons = {}
        for label, tool in TOOLS:
            b = self._button(label, (lambda t: lambda: self.set_tool(t))(tool))
            self.tool_buttons[tool] = b
            bar.Children.Add(b)
        gap = Border()
        gap.Width = 14
        bar.Children.Add(gap)
        self.colour_buttons = {}
        for name, hex_ in COLOURS:
            dot = Border()
            dot.Width = dot.Height = 16
            dot.CornerRadius = CornerRadius(8)
            dot.Background = self.brush(hex_)
            b = Button()
            b.Content = dot
            b.Margin = Thickness(0, 0, 6, 0)
            b.Padding = Thickness(7, 5, 7, 5)
            b.ToolTip = name
            b.Click += (lambda c: lambda s, a: self.set_colour(c))(hex_)
            self.colour_buttons[hex_] = b
            bar.Children.Add(b)
        gap2 = Border()
        gap2.Width = 14
        bar.Children.Add(gap2)
        bar.Children.Add(self._button("Undo", self.undo, "Undo the last mark (Ctrl+Z)"))
        bar.Children.Add(self._button("Clear", self.clear, "Remove every mark"))
        return bar

    def _footer(self):
        bar = Grid()
        bar.Margin = Thickness(0, 10, 0, 0)
        DockPanel.SetDock(bar, Dock.Bottom)
        hint = TextBlock()
        hint.Text = "Draw on the view to point things out, then send it with your comment."
        hint.Foreground = self.brush("#8A9BB3")
        hint.VerticalAlignment = VerticalAlignment.Center
        bar.Children.Add(hint)
        buttons = StackPanel()
        buttons.Orientation = Orientation.Horizontal
        buttons.HorizontalAlignment = HorizontalAlignment.Right
        cancel = self._button("Send without the view", self.cancel, "Send the comment without the picture and the view")
        send = self._button("Send", self.send, "Send the view (with your drawing) with the comment")
        primary = self.TryFindResource("Primary")
        if primary is not None:
            send.Style = primary
        send.Margin = Thickness(0)
        buttons.Children.Add(cancel)
        buttons.Children.Add(send)
        bar.Children.Add(buttons)
        return bar

    def _refresh_buttons(self):
        for tool, b in self.tool_buttons.items():
            b.BorderBrush = self.brush("#00A0E3" if tool == self.tool else "#1C2B40")
        for hex_, b in self.colour_buttons.items():
            b.BorderBrush = self.brush("#00A0E3" if hex_ == self.colour else "#1C2B40")

    # ---------- drawing ----------

    def set_tool(self, tool):
        self.tool = tool
        self._refresh_buttons()

    def set_colour(self, colour):
        self.colour = colour
        self._refresh_buttons()

    def _stroke(self, shape):
        shape.Stroke = self.brush(self.colour)
        shape.StrokeThickness = self.thickness
        shape.StrokeStartLineCap = shape.StrokeEndLineCap = PenLineCap.Round
        shape.StrokeLineJoin = PenLineJoin.Round
        shape.IsHitTestVisible = False
        self.canvas.Children.Add(shape)
        return shape

    def on_down(self, sender, args):
        p = args.GetPosition(self.canvas)
        self.start = p
        if self.tool == "pen":
            line = self._stroke(Polyline())
            line.Points = PointCollection([p])
            self.drawing = [line]
        elif self.tool == "arrow":
            line = self._stroke(Line())
            line.X1, line.Y1, line.X2, line.Y2 = p.X, p.Y, p.X, p.Y
            head = self._stroke(Polyline())
            self.drawing = [line, head]
        else:
            box = self._stroke(Rectangle())
            box.RadiusX = box.RadiusY = self.thickness * 1.5
            Canvas.SetLeft(box, p.X)
            Canvas.SetTop(box, p.Y)
            self.drawing = [box]
        self.canvas.CaptureMouse()

    def on_move(self, sender, args):
        if self.drawing is None:
            return
        p = args.GetPosition(self.canvas)
        if self.tool == "pen":
            self.drawing[0].Points.Add(p)
        elif self.tool == "arrow":
            line, head = self.drawing
            line.X2, line.Y2 = p.X, p.Y
            pts = arrow_head(self.start.X, self.start.Y, p.X, p.Y, self.thickness * 5)
            head.Points = PointCollection([Point(x, y) for x, y in pts])
        else:
            box = self.drawing[0]
            Canvas.SetLeft(box, min(p.X, self.start.X))
            Canvas.SetTop(box, min(p.Y, self.start.Y))
            box.Width, box.Height = abs(p.X - self.start.X), abs(p.Y - self.start.Y)

    def on_up(self, sender, args):
        if self.drawing is not None:
            self.shapes.append(self.drawing)
        self.drawing = None
        self.canvas.ReleaseMouseCapture()

    def undo(self):
        if self.shapes:
            for s in self.shapes.pop():
                self.canvas.Children.Remove(s)

    def clear(self):
        self.shapes = []
        self.canvas.Children.Clear()

    def on_key(self, sender, args):
        if args.Key == Key.Z and Keyboard.Modifiers == ModifierKeys.Control:
            self.undo()
            args.Handled = True
        elif args.Key == Key.Escape:
            self.cancel()

    # ---------- result ----------

    def render(self):
        """The picture with the drawing on it, at the picture's full size, as PNG bytes."""
        dv = DrawingVisual()
        dc = dv.RenderOpen()
        dc.DrawImage(self.picture, Rect(0, 0, self.w, self.h))
        if self.canvas.Children.Count:
            ink = VisualBrush(self.canvas)
            ink.ViewboxUnits = BrushMappingMode.Absolute
            ink.Viewbox = Rect(0, 0, self.w, self.h)
            ink.Stretch = Stretch.Fill
            dc.DrawRectangle(ink, None, Rect(0, 0, self.w, self.h))
        dc.Close()
        rtb = RenderTargetBitmap(self.w, self.h, 96, 96, PixelFormats.Pbgra32)
        rtb.Render(dv)
        enc = PngBitmapEncoder()
        enc.Frames.Add(BitmapFrame.Create(rtb))
        ms = MemoryStream()
        enc.Save(ms)
        return bytes(bytearray(ms.ToArray()))

    def send(self):
        self.result = self.render()
        self.Close()

    def cancel(self):
        self.result = None
        self.Close()


def markup(png, resources=None, brush=None):
    """Shows the markup window; returns the PNG to send, or None when cancelled."""
    if brush is None:
        conv = BrushConverter()
        brush = lambda c: conv.ConvertFromString(c)  # noqa: E731
    win = MarkupWindow(png, resources, brush)
    win.ShowDialog()
    return win.result
